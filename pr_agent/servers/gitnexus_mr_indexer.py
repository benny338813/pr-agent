import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

from pr_agent.log import get_logger


@dataclass(frozen=True)
class GitNexusMRPayload:
    project_id: str
    project_path: str
    repo_url: str
    mr_iid: str
    source_branch: str
    source_sha: str


@dataclass(frozen=True)
class GitNexusIndexResult:
    ready: bool
    repo_path: Path
    repo: str
    index_commit: str
    reused: bool = False
    stale: bool = False
    message: str = ""


_semaphores = {}
_semaphores_lock = threading.Lock()


def _semaphore_for(max_parallel_jobs: int) -> threading.Semaphore:
    max_parallel_jobs = max(1, max_parallel_jobs)
    with _semaphores_lock:
        if max_parallel_jobs not in _semaphores:
            _semaphores[max_parallel_jobs] = threading.Semaphore(max_parallel_jobs)
        return _semaphores[max_parallel_jobs]


class GitNexusMRIndexer:
    def __init__(
        self,
        settings,
        run_command: Optional[Callable[[List[str], Path, int], None]] = None,
        now: Optional[Callable[[], float]] = None,
    ):
        self.settings = settings
        self.run_command = run_command or self._run_command
        self.now = now or time.time

    def prepare(self, payload: GitNexusMRPayload) -> GitNexusIndexResult:
        max_parallel_jobs = int(self._get("gitnexus_indexer.max_parallel_jobs", 2))
        semaphore = _semaphore_for(max_parallel_jobs)
        with semaphore:
            return self._prepare_locked(payload)

    def _prepare_locked(self, payload: GitNexusMRPayload) -> GitNexusIndexResult:
        repo_path = self._repo_path(payload)
        sha_path = repo_path.parent
        metadata_path = sha_path / "metadata.json"
        timeout = int(self._get("gitnexus_indexer.timeout_seconds", 300))

        if self._get("gitnexus_indexer.per_mr_latest_only", True):
            self._latest_sha_path(payload).parent.mkdir(parents=True, exist_ok=True)
            self._latest_sha_path(payload).write_text(payload.source_sha, encoding="utf-8")

        if self._can_reuse(repo_path, metadata_path, payload):
            return GitNexusIndexResult(
                ready=True,
                repo_path=repo_path,
                repo=payload.project_path,
                index_commit=payload.source_sha,
                reused=True,
                message="reused existing GitNexus index",
            )

        self._ensure_checkout(repo_path, payload.repo_url, timeout)
        self.run_command(["git", "fetch", "origin", payload.source_sha], repo_path, timeout)
        self.run_command(["git", "checkout", "--force", payload.source_sha], repo_path, timeout)
        self.run_command(self._analyze_command(), repo_path, timeout)

        if self._get("gitnexus_indexer.per_mr_latest_only", True):
            latest_sha = self._latest_sha_path(payload).read_text(encoding="utf-8").strip()
            if latest_sha != payload.source_sha:
                return GitNexusIndexResult(
                    ready=False,
                    repo_path=repo_path,
                    repo=payload.project_path,
                    index_commit=payload.source_sha,
                    stale=True,
                    message=f"stale GitNexus index for {payload.source_sha}; latest MR sha is {latest_sha}",
                )

        metadata_path.write_text(
            json.dumps({
                "project_id": payload.project_id,
                "project_path": payload.project_path,
                "mr_iid": payload.mr_iid,
                "source_branch": payload.source_branch,
                "source_sha": payload.source_sha,
                "ready": True,
                "created_at": self.now(),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return GitNexusIndexResult(
            ready=True,
            repo_path=repo_path,
            repo=payload.project_path,
            index_commit=payload.source_sha,
            message="created GitNexus index",
        )

    def cleanup_expired(self) -> List[Path]:
        ttl_hours = float(self._get("gitnexus_indexer.ttl_hours", 72))
        if ttl_hours <= 0:
            return []

        root = self._workspace_root()
        if not root.exists():
            return []

        cutoff = self.now() - ttl_hours * 3600
        removed = []
        for sha_dir in root.glob("*/*/*"):
            if not sha_dir.is_dir():
                continue
            try:
                if sha_dir.stat().st_mtime < cutoff:
                    shutil.rmtree(sha_dir)
                    removed.append(sha_dir)
            except OSError as e:
                get_logger().warning(f"Failed to remove expired GitNexus workspace {sha_dir}: {e}")
        return removed

    def _can_reuse(self, repo_path: Path, metadata_path: Path, payload: GitNexusMRPayload) -> bool:
        if not self._get("gitnexus_indexer.reuse_existing_index", True):
            return False
        if not (repo_path / ".gitnexus").exists() or not metadata_path.exists():
            return False
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return metadata.get("ready") is True and metadata.get("source_sha") == payload.source_sha

    def _ensure_checkout(self, repo_path: Path, repo_url: str, timeout: int) -> None:
        repo_path.mkdir(parents=True, exist_ok=True)
        if (repo_path / ".git").exists():
            return
        if any(repo_path.iterdir()):
            shutil.rmtree(repo_path)
            repo_path.mkdir(parents=True, exist_ok=True)
        self.run_command(["git", "clone", "--no-checkout", repo_url, str(repo_path)], repo_path, timeout)

    def _repo_path(self, payload: GitNexusMRPayload) -> Path:
        return self._workspace_root() / payload.project_id / payload.mr_iid / payload.source_sha / "repo"

    def _latest_sha_path(self, payload: GitNexusMRPayload) -> Path:
        return self._workspace_root() / payload.project_id / payload.mr_iid / "latest_sha"

    def _workspace_root(self) -> Path:
        return Path(str(self._get("gitnexus_indexer.workspace_root", ".gitnexus-workspaces"))).expanduser()

    def _analyze_command(self) -> List[str]:
        command = str(self._get("gitnexus_indexer.analyze_command", "npx"))
        args = list(self._get("gitnexus_indexer.analyze_args", ["gitnexus", "analyze", "."]))
        return [command, *args]

    def _get(self, key: str, default):
        return self.settings.get(key, default)

    @staticmethod
    def _run_command(args: List[str], cwd: Path, timeout: int) -> None:
        subprocess.run(
            args,
            cwd=str(cwd),
            timeout=timeout,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
