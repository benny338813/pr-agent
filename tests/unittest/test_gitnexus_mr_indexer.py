import json
import os
import time
from pathlib import Path
from unittest import mock

os.environ.setdefault("GITLAB__URL", "https://gitlab.example.com")

import pytest

import pr_agent.servers.gitlab_webhook as gitlab_webhook
from pr_agent.servers.gitnexus_mr_indexer import GitNexusMRIndexer, GitNexusMRPayload


def _settings(values):
    class FakeSettings:
        def get(self, key, default=None):
            return values.get(key, default)

        def set(self, key, value):
            values[key] = value

    return FakeSettings()


def _payload(source_sha="abc123"):
    return GitNexusMRPayload(
        project_id="42",
        project_path="group/repo",
        repo_url="https://gitlab.example.com/group/repo.git",
        mr_iid="7",
        source_branch="feature/test",
        source_sha=source_sha,
    )


def test_indexer_uses_isolated_workspace_per_project_mr_and_sha(tmp_path):
    settings = _settings({
        "gitnexus_indexer.workspace_root": str(tmp_path),
        "gitnexus_indexer.analyze_command": "npx",
        "gitnexus_indexer.analyze_args": ["gitnexus", "analyze", "."],
        "gitnexus_indexer.timeout_seconds": 120,
        "gitnexus_indexer.reuse_existing_index": False,
    })
    calls = []

    def runner(args, cwd, timeout):
        calls.append((args, Path(cwd), timeout))
        if args[0] == "git" and args[1] == "clone":
            Path(cwd).mkdir(parents=True, exist_ok=True)
        if args[:3] == ["npx", "gitnexus", "analyze"]:
            (Path(cwd) / ".gitnexus").mkdir()

    result = GitNexusMRIndexer(settings, run_command=runner).prepare(_payload())

    assert result.ready is True
    assert result.stale is False
    assert result.repo_path == tmp_path / "42" / "7" / "abc123" / "repo"
    assert result.index_commit == "abc123"
    assert calls == [
        (["git", "clone", "--no-checkout", "https://gitlab.example.com/group/repo.git", str(result.repo_path)], result.repo_path, 120),
        (["git", "fetch", "origin", "abc123"], result.repo_path, 120),
        (["git", "checkout", "--force", "abc123"], result.repo_path, 120),
        (["npx", "gitnexus", "analyze", "."], result.repo_path, 120),
    ]


def test_indexer_reuses_existing_index_for_same_sha(tmp_path):
    repo_path = tmp_path / "42" / "7" / "abc123" / "repo"
    (repo_path / ".gitnexus").mkdir(parents=True)
    metadata_path = repo_path.parent / "metadata.json"
    metadata_path.write_text(json.dumps({"source_sha": "abc123", "ready": True}), encoding="utf-8")
    settings = _settings({
        "gitnexus_indexer.workspace_root": str(tmp_path),
        "gitnexus_indexer.reuse_existing_index": True,
    })

    result = GitNexusMRIndexer(settings, run_command=mock.Mock()).prepare(_payload())

    assert result.ready is True
    assert result.reused is True
    assert result.repo_path == repo_path


def test_indexer_marks_old_job_stale_when_new_sha_arrives(tmp_path):
    settings = _settings({
        "gitnexus_indexer.workspace_root": str(tmp_path),
        "gitnexus_indexer.analyze_command": "npx",
        "gitnexus_indexer.analyze_args": ["gitnexus", "analyze", "."],
        "gitnexus_indexer.per_mr_latest_only": True,
    })

    def runner(args, cwd, timeout):
        Path(cwd).mkdir(parents=True, exist_ok=True)
        latest_path = tmp_path / "42" / "7" / "latest_sha"
        if args[:3] == ["npx", "gitnexus", "analyze"]:
            latest_path.write_text("new456", encoding="utf-8")
            (Path(cwd) / ".gitnexus").mkdir()

    result = GitNexusMRIndexer(settings, run_command=runner).prepare(_payload("old123"))

    assert result.ready is False
    assert result.stale is True


def test_indexer_cleans_expired_workspaces(tmp_path):
    old_repo = tmp_path / "42" / "7" / "oldsha" / "repo"
    fresh_repo = tmp_path / "42" / "7" / "freshsha" / "repo"
    old_repo.mkdir(parents=True)
    fresh_repo.mkdir(parents=True)
    old_time = time.time() - 10 * 3600
    os.utime(old_repo.parent, (old_time, old_time))
    settings = _settings({
        "gitnexus_indexer.workspace_root": str(tmp_path),
        "gitnexus_indexer.ttl_hours": 1,
    })

    removed = GitNexusMRIndexer(settings).cleanup_expired()

    assert old_repo.parent in removed
    assert not old_repo.exists()
    assert fresh_repo.exists()


@pytest.mark.asyncio
async def test_gitlab_webhook_prepares_gitnexus_and_sets_review_runtime_config(monkeypatch, tmp_path):
    settings = _settings({
        "gitnexus_indexer.enabled": True,
        "gitnexus_indexer.mcp_command": "npx",
        "gitnexus_indexer.mcp_args": ["gitnexus", "mcp"],
        "gitnexus_indexer.max_queries": 6,
        "gitnexus_indexer.max_symbols_per_file": 3,
    })
    result = type("Result", (), {
        "ready": True,
        "stale": False,
        "repo_path": tmp_path / "42" / "7" / "abc123" / "repo",
        "repo": "group/repo",
        "index_commit": "abc123",
        "reused": False,
        "message": "ok",
    })()

    class FakeIndexer:
        def __init__(self, settings):
            self.settings = settings

        def cleanup_expired(self):
            return []

        def prepare(self, payload):
            assert payload.project_id == "42"
            assert payload.mr_iid == "7"
            assert payload.source_sha == "abc123"
            return result

    monkeypatch.setattr(gitlab_webhook, "get_settings", lambda: settings)
    monkeypatch.setattr(gitlab_webhook, "GitNexusMRIndexer", FakeIndexer)

    prepared = await gitlab_webhook._prepare_gitnexus_for_mr({
        "project": {
            "id": 42,
            "path_with_namespace": "group/repo",
            "git_http_url": "https://gitlab.example.com/group/repo.git",
        },
        "object_attributes": {
            "iid": 7,
            "source_branch": "feature/test",
            "last_commit": {"id": "abc123"},
        },
    })

    assert prepared is result
    assert settings.get("gitnexus") == {
        "enabled": True,
        "command": "npx",
        "args": ["gitnexus", "mcp"],
        "mode": "pr_head_context",
        "repo": "group/repo",
        "working_dir": str(result.repo_path),
        "index_ref": "feature/test",
        "index_commit": "abc123",
        "max_queries": 6,
        "max_symbols_per_file": 3,
        "fail_on_error": False,
    }


@pytest.mark.asyncio
async def test_gitlab_webhook_skips_gitnexus_when_disabled(monkeypatch):
    settings = _settings({"gitnexus_indexer.enabled": False})
    monkeypatch.setattr(gitlab_webhook, "get_settings", lambda: settings)

    assert await gitlab_webhook._prepare_gitnexus_for_mr({}) is None
