import pytest

from pr_agent.algo.types import EDIT_TYPE, FilePatchInfo
import pr_agent.tools.pr_reviewer as pr_reviewer
from pr_agent.tools.pr_reviewer import PRReviewer


def _settings(mcp_config=None, gitnexus_config=None):
    return type("Settings", (), {
        "get": lambda self, key, default=None: (
            mcp_config if key == "mcp" else
            gitnexus_config if key == "gitnexus" else
            False if key == "gitnexus.fail_on_error" else
            default
        ),
    })()


def test_mcp_config_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings(None))

    assert PRReviewer._get_mcp_config(object()) is None


def test_mcp_config_requires_explicit_enabled(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings({
        "command": "node",
        "args": ["server.js"],
    }))

    assert PRReviewer._get_mcp_config(object()) is None


def test_mcp_config_returns_command_and_args_when_enabled(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings({
        "enabled": True,
        "command": "node",
        "args": ["server.js"],
    }))

    assert PRReviewer._get_mcp_config(object()) == {"command": "node", "args": ["server.js"]}


def test_mcp_config_rejects_missing_command(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings({
        "enabled": True,
        "args": ["server.js"],
    }))

    with pytest.raises(ValueError, match="mcp.command"):
        PRReviewer._get_mcp_config(object())


def test_mcp_config_rejects_non_list_args(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings({
        "enabled": True,
        "command": "node",
        "args": "server.js",
    }))

    with pytest.raises(ValueError, match="mcp.args"):
        PRReviewer._get_mcp_config(object())


def test_gitnexus_config_is_disabled_by_default(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings(gitnexus_config=None))

    assert PRReviewer._get_gitnexus_config(object()) is None


def test_gitnexus_config_requires_explicit_enabled(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings(gitnexus_config={
        "command": "node",
        "args": ["gitnexus.js", "mcp"],
    }))

    assert PRReviewer._get_gitnexus_config(object()) is None


def test_gitnexus_config_returns_command_args_and_options_when_enabled(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings(gitnexus_config={
        "enabled": True,
        "command": "node",
        "args": ["gitnexus.js", "mcp"],
        "mode": "base_context",
        "repo": "nvme-cli",
        "scope": "compare",
        "base_ref": "master",
        "index_ref": "develop-stable",
        "index_commit": "abc123",
        "max_queries": 3,
        "max_symbols_per_file": 1,
    }))

    assert PRReviewer._get_gitnexus_config(object()) == {
        "command": "node",
        "args": ["gitnexus.js", "mcp"],
        "mode": "base_context",
        "repo": "nvme-cli",
        "scope": "compare",
        "base_ref": "master",
        "index_ref": "develop-stable",
        "index_commit": "abc123",
        "max_queries": 3,
        "max_symbols_per_file": 1,
    }


def test_gitnexus_config_rejects_missing_command(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings(gitnexus_config={
        "enabled": True,
        "args": ["mcp"],
    }))

    with pytest.raises(ValueError, match="gitnexus.command"):
        PRReviewer._get_gitnexus_config(object())


def test_format_gitnexus_context_prefers_text_content():
    assert PRReviewer._format_gitnexus_context([
        {"type": "text", "text": "Changes: 1 files"},
        {"type": "json", "value": {"risk": "low"}},
    ]) == 'Changes: 1 files\n{"type": "json", "value": {"risk": "low"}}'


def test_build_gitnexus_base_context_candidates_marks_added_files_as_snapshot_queries():
    reviewer = PRReviewer.__new__(PRReviewer)
    diff_files = [
        FilePatchInfo(
            base_file="",
            head_file="int validate_new_flow(void) { return 0; }",
            patch="@@ -0,0 +1,3 @@\n+int validate_new_flow(void) {\n+    return 0;\n+}",
            filename="src/new_flow.c",
            edit_type=EDIT_TYPE.ADDED,
        )
    ]

    candidates = reviewer._build_gitnexus_base_context_candidates(diff_files, max_files=5, max_symbols_per_file=2)

    assert candidates[0]["edit_type"] == "ADDED"
    assert candidates[0]["base_file_path"] == "src/new_flow.c"
    assert "validate_new_flow" in candidates[0]["query"]


def test_gitnexus_base_context_header_warns_snapshot_is_not_pr_head(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: type("Settings", (), {
        "config": type("Config", (), {"git_provider": "gitlab"})(),
    })())

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = type("Provider", (), {
        "mr": type("MergeRequest", (), {"target_branch": "develop"})(),
    })()
    header = reviewer._format_gitnexus_base_context_header({
        "base_ref": "",
        "index_ref": "develop-stable",
        "index_commit": "abc123",
    })

    assert "not the PR source branch" in header
    assert "Indexed ref: develop-stable" in header
    assert "Do not treat missing GitNexus results" in header


def test_gitnexus_base_ref_prefers_configured_value(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: type("Settings", (), {
        "config": type("Config", (), {"git_provider": "gitlab"})(),
    })())

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = type("Provider", (), {
        "mr": type("MergeRequest", (), {"target_branch": "develop"})(),
    })()
    reviewer.pr_url = "https://gitlab.example.com/group/repo/-/merge_requests/1"

    assert reviewer._get_gitnexus_base_ref("main") == "main"


def test_gitnexus_base_ref_uses_gitlab_target_branch(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: type("Settings", (), {
        "config": type("Config", (), {"git_provider": "gitlab"})(),
    })())

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = type("Provider", (), {
        "mr": type("MergeRequest", (), {"target_branch": "develop"})(),
    })()
    reviewer.pr_url = "https://gitlab.example.com/group/repo/-/merge_requests/1"

    assert reviewer._get_gitnexus_base_ref("") == "develop"


def test_gitnexus_base_ref_uses_local_pr_url(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: type("Settings", (), {
        "config": type("Config", (), {"git_provider": "local"})(),
    })())

    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = type("Provider", (), {})()
    reviewer.pr_url = "master"

    assert reviewer._get_gitnexus_base_ref("") == "master"


@pytest.mark.asyncio
async def test_gitnexus_context_calls_detect_changes(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings(gitnexus_config={
        "enabled": True,
        "command": "node",
        "args": ["gitnexus.js", "mcp"],
        "repo": "nvme-cli",
        "scope": "compare",
    }))

    class FakeMCPHandler:
        last_args = None

        def __init__(self, command, args):
            self.command = command
            self.args = args

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def get_openai_tools(self):
            return [{"function": {"name": "detect_changes"}}]

        async def call_tool(self, name, arguments):
            assert name == "detect_changes"
            FakeMCPHandler.last_args = arguments
            return [{"type": "text", "text": "Changes: 1 files"}]

    import pr_agent.algo.mcp_handler as mcp_handler

    monkeypatch.setattr(mcp_handler, "MCPHandler", FakeMCPHandler)
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = type("Provider", (), {
        "mr": type("MergeRequest", (), {"target_branch": "master"})(),
    })()
    reviewer.pr_url = "master"

    context = await reviewer._get_gitnexus_context()

    assert FakeMCPHandler.last_args == {"scope": "compare", "repo": "nvme-cli", "base_ref": "master"}
    assert "Changes: 1 files" in context


@pytest.mark.asyncio
async def test_gitnexus_base_context_uses_query_context_and_impact(monkeypatch):
    monkeypatch.setattr(pr_reviewer, "get_settings", lambda: _settings(gitnexus_config={
        "enabled": True,
        "command": "node",
        "args": ["gitnexus.js", "mcp"],
        "mode": "base_context",
        "repo": "nvme-cli",
        "index_ref": "develop-stable",
        "index_commit": "abc123",
        "max_queries": 2,
        "max_symbols_per_file": 1,
    }))

    class FakeMCPHandler:
        calls = []

        def __init__(self, command, args):
            self.command = command
            self.args = args

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            pass

        async def get_openai_tools(self):
            return [
                {"function": {"name": "query"}},
                {"function": {"name": "context"}},
                {"function": {"name": "impact"}},
            ]

        async def call_tool(self, name, arguments):
            FakeMCPHandler.calls.append((name, arguments))
            return [{"type": "text", "text": f"{name} result"}]

    import pr_agent.algo.mcp_handler as mcp_handler

    monkeypatch.setattr(mcp_handler, "MCPHandler", FakeMCPHandler)
    reviewer = PRReviewer.__new__(PRReviewer)
    reviewer.git_provider = type("Provider", (), {
        "mr": type("MergeRequest", (), {"target_branch": "develop"})(),
        "get_diff_files": lambda self: [
            FilePatchInfo(
                base_file="",
                head_file="",
                patch="@@ -1,3 +1,3 @@ fabrics_connect\n- old\n+ new",
                filename="fabrics.c",
                edit_type=EDIT_TYPE.MODIFIED,
            ),
            FilePatchInfo(
                base_file="",
                head_file="",
                patch="@@ -0,0 +1,2 @@\n+int new_symbol(void) { return 0; }",
                filename="new.c",
                edit_type=EDIT_TYPE.ADDED,
            ),
        ],
    })()
    reviewer.pr_url = "https://gitlab.example.com/group/repo/-/merge_requests/1"

    context = await reviewer._get_gitnexus_context()

    call_names = [name for name, _ in FakeMCPHandler.calls]
    assert call_names == ["context", "impact", "query", "query"]
    assert FakeMCPHandler.calls[0][1]["file_path"] == "fabrics.c"
    assert FakeMCPHandler.calls[-1][1]["query"].startswith("new.c")
    assert "Source: indexed stable/base snapshot" in context
    assert "query result" in context
