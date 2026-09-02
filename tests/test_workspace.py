"""Workspace isolation: clean checkouts, diffs, and cleanup."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from acb.runner.workspace import (
    WorkspaceError,
    create_workspace,
    prune_workspaces,
    resolve_commit,
)


def test_worktree_starts_clean_at_head(git_fixture_repo: Path, tmp_path: Path):
    with create_workspace(git_fixture_repo, None, root=tmp_path / "ws") as ws:
        assert ws.path.exists()
        assert ws.is_clean()
        assert (ws.path / "calc.py").exists()
        assert (ws.path / "notes.md").exists()


def test_checks_out_the_exact_requested_commit(git_fixture_repo: Path, tmp_path: Path):
    first = subprocess.run(
        ["git", "-C", str(git_fixture_repo), "rev-list", "--max-parents=0", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    with create_workspace(git_fixture_repo, first, root=tmp_path / "ws") as ws:
        assert ws.base_commit == first
        # notes.md only exists in the second commit.
        assert not (ws.path / "notes.md").exists()


def test_refs_are_resolved_to_full_shas(git_fixture_repo: Path):
    sha = resolve_commit(git_fixture_repo, "main")
    assert len(sha) == 40
    assert sha == resolve_commit(git_fixture_repo, None)


def test_runs_are_isolated_from_each_other(git_fixture_repo: Path, tmp_path: Path):
    with create_workspace(git_fixture_repo, None, root=tmp_path / "ws") as a, \
         create_workspace(git_fixture_repo, None, root=tmp_path / "ws") as b:
        assert a.path != b.path
        (a.path / "calc.py").write_text("changed in a\n")
        assert (b.path / "calc.py").read_text() != "changed in a\n"


def test_source_repository_is_never_modified(git_fixture_repo: Path, tmp_path: Path):
    before = (git_fixture_repo / "calc.py").read_text()
    with create_workspace(git_fixture_repo, None, root=tmp_path / "ws") as ws:
        (ws.path / "calc.py").write_text("agent edit\n")
        (ws.path / "new_file.py").write_text("new\n")
        ws.diff()
    assert (git_fixture_repo / "calc.py").read_text() == before
    assert not (git_fixture_repo / "new_file.py").exists()
    dirty = subprocess.run(
        ["git", "-C", str(git_fixture_repo), "status", "--porcelain"],
        capture_output=True, text=True,
    ).stdout.strip()
    assert dirty == ""


def test_diff_includes_modified_and_untracked_files(git_fixture_repo: Path, tmp_path: Path):
    with create_workspace(git_fixture_repo, None, root=tmp_path / "ws") as ws:
        (ws.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (ws.path / "brand_new.py").write_text("x = 1\n")
        diff = ws.diff()
        assert "calc.py" in diff
        assert "brand_new.py" in diff, "untracked files must appear in the captured diff"


def test_diff_stat_counts_files_and_lines(git_fixture_repo: Path, tmp_path: Path):
    with create_workspace(git_fixture_repo, None, root=tmp_path / "ws") as ws:
        (ws.path / "calc.py").write_text("def add(a, b):\n    return a + b\n")
        (ws.path / "extra.py").write_text("y = 2\n")
        files, added, removed = ws.diff_stat()
        assert files == 2
        assert added >= 2
        assert removed >= 1
        assert set(ws.changed_files()) == {"calc.py", "extra.py"}


def test_no_changes_yields_an_empty_diff(git_fixture_repo: Path, tmp_path: Path):
    with create_workspace(git_fixture_repo, None, root=tmp_path / "ws") as ws:
        assert ws.diff().strip() == ""
        assert ws.diff_stat() == (0, 0, 0)


def test_cleanup_removes_the_workspace(git_fixture_repo: Path, tmp_path: Path):
    ws = create_workspace(git_fixture_repo, None, root=tmp_path / "ws")
    path = ws.path
    ws.cleanup()
    assert not path.exists()
    worktrees = subprocess.run(
        ["git", "-C", str(git_fixture_repo), "worktree", "list"],
        capture_output=True, text=True,
    ).stdout
    assert str(path) not in worktrees


def test_cleanup_is_idempotent(git_fixture_repo: Path, tmp_path: Path):
    ws = create_workspace(git_fixture_repo, None, root=tmp_path / "ws")
    ws.cleanup()
    ws.cleanup()  # must not raise


def test_clone_temp_mode(git_fixture_repo: Path, tmp_path: Path):
    with create_workspace(
        git_fixture_repo, None, mode="clone_temp", root=tmp_path / "ws"
    ) as ws:
        assert ws.mode == "clone_temp"
        assert (ws.path / ".git").exists()
        assert ws.is_clean()


def test_container_mode_is_explicitly_unimplemented(git_fixture_repo: Path, tmp_path: Path):
    with pytest.raises(WorkspaceError, match="not implemented"):
        create_workspace(git_fixture_repo, None, mode="container", root=tmp_path / "ws")


def test_unknown_mode_rejected(git_fixture_repo: Path, tmp_path: Path):
    with pytest.raises(WorkspaceError, match="unknown isolation mode"):
        create_workspace(git_fixture_repo, None, mode="magic", root=tmp_path / "ws")


def test_missing_repository_rejected(tmp_path: Path):
    with pytest.raises(WorkspaceError, match="does not exist"):
        create_workspace(tmp_path / "nope", None, root=tmp_path / "ws")


def test_non_git_directory_rejected(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorkspaceError, match="not a git repository"):
        create_workspace(plain, None, root=tmp_path / "ws")


def test_unknown_commit_rejected(git_fixture_repo: Path, tmp_path: Path):
    with pytest.raises(WorkspaceError):
        create_workspace(git_fixture_repo, "deadbeef" * 5, root=tmp_path / "ws")


def test_prune_removes_leftovers(tmp_path: Path):
    root = tmp_path / "leftovers"
    root.mkdir()
    (root / "abandoned-1").mkdir()
    (root / "abandoned-2").mkdir()
    assert prune_workspaces(root) == 2
    assert list(root.iterdir()) == []


def test_prune_on_missing_root_is_safe(tmp_path: Path):
    assert prune_workspaces(tmp_path / "never-existed") == 0
