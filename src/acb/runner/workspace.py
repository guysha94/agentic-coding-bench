"""Workspace isolation.

Every run gets a throwaway, verified-clean checkout of the fixture repository at an exact
commit. Git worktrees are the default: fast (shared object store, no re-clone), genuinely
clean, and safe to run in parallel.

Escalation path: worktree -> clone_temp (for tasks that mutate git state) -> container
(reserved for hostile or dependency-heavy tasks).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path


class WorkspaceError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path | str | None = None, timeout: int = 120) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


def resolve_commit(repo: Path, ref: str | None) -> str:
    """Resolve a ref (or HEAD) to a full SHA, so the run records exactly what it ran."""
    return _git(["rev-parse", ref or "HEAD"], cwd=repo).strip()


@dataclass
class Workspace:
    """An isolated checkout. Use as a context manager so cleanup always happens."""

    path: Path
    source_repo: Path
    base_commit: str
    mode: str = "worktree"
    _cleaned: bool = field(default=False, repr=False)

    # ------------------------------------------------------------------ inspection -----
    def is_clean(self) -> bool:
        return _git(["status", "--porcelain"], cwd=self.path).strip() == ""

    def assert_clean(self) -> None:
        dirty = _git(["status", "--porcelain"], cwd=self.path).strip()
        if dirty:
            raise WorkspaceError(
                f"workspace {self.path} is not clean before the agent starts:\n{dirty}"
            )

    def diff(self) -> str:
        """Full diff of the agent's work, including untracked files."""
        # Stage everything so new files appear in the diff, then diff against the base
        # commit. `git add -A` touches only the disposable workspace index.
        _git(["add", "-A"], cwd=self.path)
        return _git(["diff", "--cached", self.base_commit], cwd=self.path, timeout=180)

    def diff_stat(self) -> tuple[int, int, int]:
        """(files_changed, lines_added, lines_removed) for the agent's work."""
        _git(["add", "-A"], cwd=self.path)
        out = _git(["diff", "--cached", "--numstat", self.base_commit], cwd=self.path)
        files = added = removed = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            files += 1
            for raw, key in ((parts[0], "a"), (parts[1], "r")):
                if raw == "-":  # binary file
                    continue
                if key == "a":
                    added += int(raw)
                else:
                    removed += int(raw)
        return files, added, removed

    def changed_files(self) -> list[str]:
        _git(["add", "-A"], cwd=self.path)
        out = _git(["diff", "--cached", "--name-only", self.base_commit], cwd=self.path)
        return [line for line in out.splitlines() if line.strip()]

    # ------------------------------------------------------------------ lifecycle ------
    def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        try:
            if self.mode == "worktree":
                subprocess.run(
                    ["git", "worktree", "remove", "--force", str(self.path)],
                    cwd=str(self.source_repo),
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                subprocess.run(
                    ["git", "worktree", "prune"],
                    cwd=str(self.source_repo),
                    capture_output=True,
                    timeout=60,
                )
            if self.path.exists():
                shutil.rmtree(self.path, ignore_errors=True)
        except Exception:
            # Cleanup must never mask the run's real result; leftover dirs are visible in
            # the scratch root and prune-able.
            if self.path.exists():
                shutil.rmtree(self.path, ignore_errors=True)

    def __enter__(self) -> Workspace:
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()


def create_workspace(
    repo_path: Path,
    base_commit: str | None,
    mode: str = "worktree",
    root: Path | None = None,
    label: str = "run",
) -> Workspace:
    """Create an isolated checkout of `repo_path` at `base_commit`."""
    repo = repo_path.resolve()
    if not repo.exists():
        raise WorkspaceError(f"fixture repository does not exist: {repo}")
    if not (repo / ".git").exists():
        raise WorkspaceError(
            f"fixture path is not a git repository: {repo} "
            "(fixtures must be git repos so runs can start from an exact commit)"
        )

    commit = resolve_commit(repo, base_commit)
    root = root or Path(tempfile.gettempdir()) / "acb-workspaces"
    root.mkdir(parents=True, exist_ok=True)
    dest = root / f"{label}-{uuid.uuid4().hex[:10]}"

    if mode == "worktree":
        _git(["worktree", "add", "--detach", str(dest), commit], cwd=repo, timeout=300)
    elif mode == "clone_temp":
        _git(["clone", "--no-hardlinks", "--quiet", str(repo), str(dest)], timeout=600)
        _git(["checkout", "--detach", commit], cwd=dest, timeout=300)
    elif mode == "container":
        raise WorkspaceError(
            "isolation mode 'container' is not implemented; use 'worktree' or 'clone_temp' "
            "(see docs/limitations.md)"
        )
    else:
        raise WorkspaceError(f"unknown isolation mode {mode!r}")

    ws = Workspace(path=dest, source_repo=repo, base_commit=commit, mode=mode)
    ws.assert_clean()
    return ws


def prune_workspaces(root: Path | None = None) -> int:
    """Remove leftover workspace directories from crashed runs."""
    root = root or Path(tempfile.gettempdir()) / "acb-workspaces"
    if not root.exists():
        return 0
    removed = 0
    for p in root.iterdir():
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
            removed += 1
    return removed
