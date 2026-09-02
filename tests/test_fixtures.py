"""Fixture materialisation from git bundles.

Fixtures must be real git repositories (runs start from an exact commit, and task-specific
starting states live on branches), but a nested git repo cannot be committed into the outer
one -- git records a gitlink and a fresh clone gets empty directories. Bundles solve that,
and these tests guard the round trip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from acb.fixtures import (
    FixtureError,
    bundle_paths,
    materialize,
    missing_fixtures,
    refresh_bundles,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def bundled_project(tmp_path: Path, git_fixture_repo: Path) -> Path:
    """A project root containing one fixture bundle and no materialised fixture."""
    # Give the source repo an extra branch, so branch restoration is actually exercised.
    subprocess.run(
        ["git", "-C", str(git_fixture_repo), "branch", "bug/example"],
        check=True, capture_output=True,
    )
    root = tmp_path / "project"
    (root / "fixtures" / "bundles").mkdir(parents=True)
    subprocess.run(
        ["git", "-C", str(git_fixture_repo), "bundle", "create",
         str((root / "fixtures" / "bundles" / "demo.bundle").resolve()), "--all"],
        check=True, capture_output=True,
    )
    return root


class TestMaterialization:
    def test_bundle_is_discovered(self, bundled_project: Path):
        assert [p.stem for p in bundle_paths(bundled_project)] == ["demo"]

    def test_missing_fixture_is_reported_before_materialising(self, bundled_project: Path):
        assert missing_fixtures(bundled_project) == ["demo"]

    def test_materialize_creates_a_working_repository(self, bundled_project: Path):
        statuses = materialize(bundled_project)
        assert len(statuses) == 1
        status = statuses[0]
        assert status.materialized is True
        assert (status.path / ".git").exists()
        assert (status.path / "calc.py").exists()
        assert missing_fixtures(bundled_project) == []

    def test_branches_are_restored_as_local_branches(self, bundled_project: Path):
        """`base_commit: bug/example` must resolve, which remote-tracking refs do not."""
        status = materialize(bundled_project)[0]
        assert "bug/example" in status.branches
        assert "main" in status.branches
        resolved = subprocess.run(
            ["git", "-C", str(status.path), "rev-parse", "bug/example"],
            capture_output=True, text=True,
        )
        assert resolved.returncode == 0, resolved.stderr
        assert len(resolved.stdout.strip()) == 40

    def test_bundle_remote_is_removed(self, bundled_project: Path):
        """The bundle is a file, not a live remote; leaving it invites confusing fetches."""
        status = materialize(bundled_project)[0]
        remotes = subprocess.run(
            ["git", "-C", str(status.path), "remote"], capture_output=True, text=True
        ).stdout.strip()
        assert remotes == ""

    def test_materialized_repository_is_clean(self, bundled_project: Path):
        status = materialize(bundled_project)[0]
        dirty = subprocess.run(
            ["git", "-C", str(status.path), "status", "--porcelain"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert dirty == ""

    def test_existing_fixture_is_not_clobbered(self, bundled_project: Path):
        status = materialize(bundled_project)[0]
        (status.path / "local-work.txt").write_text("do not delete me\n")

        again = materialize(bundled_project)[0]
        assert again.materialized is False
        assert (status.path / "local-work.txt").exists()

    def test_force_re_clones(self, bundled_project: Path):
        status = materialize(bundled_project)[0]
        (status.path / "local-work.txt").write_text("x\n")
        again = materialize(bundled_project, force=True)[0]
        assert again.materialized is True
        assert not (status.path / "local-work.txt").exists()

    def test_no_bundles_is_not_an_error(self, tmp_path: Path):
        assert materialize(tmp_path) == []
        assert missing_fixtures(tmp_path) == []


class TestRefresh:
    def test_round_trip_preserves_history_and_branches(self, bundled_project: Path):
        original = materialize(bundled_project)[0]
        head = subprocess.run(
            ["git", "-C", str(original.path), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()

        refresh_bundles(bundled_project)
        import shutil

        shutil.rmtree(original.path)
        restored = materialize(bundled_project)[0]

        assert set(restored.branches) == set(original.branches)
        new_head = subprocess.run(
            ["git", "-C", str(restored.path), "rev-parse", "HEAD"],
            capture_output=True, text=True,
        ).stdout.strip()
        assert new_head == head

    def test_refresh_refuses_a_dirty_fixture(self, bundled_project: Path):
        """A bundle generated from a dirty repo would not match what runs actually use."""
        status = materialize(bundled_project)[0]
        (status.path / "calc.py").write_text("uncommitted change\n")
        with pytest.raises(FixtureError, match="uncommitted changes"):
            refresh_bundles(bundled_project)


class TestShippedBundles:
    def test_repository_ships_bundles_for_every_fixture(self):
        names = {p.stem for p in bundle_paths(REPO_ROOT)}
        assert {"payment-service", "devops-stack"} <= names

    def test_shipped_fixtures_are_materialised_here(self):
        """If this fails, run `benchmark init`."""
        assert missing_fixtures(REPO_ROOT) == []

    def test_task_base_commits_resolve_in_the_materialised_fixtures(self):
        """Every `base_commit` a task names must be resolvable, or the task cannot run."""
        from acb.config import Workspace

        ws = Workspace.load(REPO_ROOT)
        for task in ws.tasks.values():
            repo = (REPO_ROOT / task.repository.path).resolve()
            ref = task.repository.base_commit or "HEAD"
            resolved = subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                capture_output=True, text=True,
            )
            assert resolved.returncode == 0, (
                f"task {task.id}: base_commit {ref!r} does not resolve in {repo.name}"
            )
