"""Fixture materialisation from git bundles.

Fixture repositories must be real git repositories -- runs start from an exact commit, and
task-specific starting states live on branches. But a nested git repository cannot be
committed into the outer repository: git records it as a gitlink, and a fresh clone would
get empty fixture directories and an unusable benchmark.

So fixtures are distributed as **git bundles** (`fixtures/bundles/*.bundle`), which capture
full history and every branch in one tracked file, and are unpacked into working
repositories on `benchmark init`.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


class FixtureError(RuntimeError):
    pass


def _git(args: list[str], cwd: Path | None = None, timeout: int = 300) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise FixtureError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


@dataclass
class FixtureStatus:
    name: str
    path: Path
    bundle: Path
    existed: bool
    materialized: bool
    branches: list[str]


def bundle_paths(root: Path) -> list[Path]:
    bundles = root / "fixtures" / "bundles"
    return sorted(bundles.glob("*.bundle")) if bundles.exists() else []


def materialize(root: Path, force: bool = False) -> list[FixtureStatus]:
    """Unpack every fixture bundle into `fixtures/<name>/`.

    Existing repositories are left untouched unless `force` is set -- re-cloning would
    silently discard an operator's local fixture changes.
    """
    results: list[FixtureStatus] = []
    for bundle in bundle_paths(root):
        name = bundle.stem
        dest = root / "fixtures" / name
        existed = (dest / ".git").exists()

        if existed and not force:
            results.append(
                FixtureStatus(name, dest, bundle, True, False, _local_branches(dest))
            )
            continue

        if existed and force:
            import shutil

            shutil.rmtree(dest)

        _git(["clone", "--quiet", str(bundle), str(dest)])
        # A bundle clone lands branches as remote-tracking refs, so `base_commit:
        # bug/idempotency` would not resolve. Promote each to a local branch.
        for ref in _remote_branches(dest):
            if ref != "main":
                _git(["branch", "--force", ref, f"origin/{ref}"], cwd=dest)
        # The bundle is not a live remote; drop it so nobody tries to fetch from it.
        _git(["remote", "remove", "origin"], cwd=dest)
        _git(["checkout", "--quiet", _default_branch(dest)], cwd=dest)

        results.append(
            FixtureStatus(name, dest, bundle, False, True, _local_branches(dest))
        )
    return results


def _remote_branches(repo: Path) -> list[str]:
    """Branch names under refs/remotes/origin/, excluding the symbolic HEAD.

    `git branch -r --format=%(refname:short)` renders refs/remotes/origin/HEAD as bare
    "origin", which is not a branch. for-each-ref with an explicit strip avoids that.
    """
    out = _git(
        [
            "for-each-ref",
            "--format=%(refname:lstrip=3)%09%(symref)",
            "refs/remotes/origin/",
        ],
        cwd=repo,
    )
    names = []
    for line in out.splitlines():
        name, _, symref = line.partition("\t")
        name = name.strip()
        if not name or name == "HEAD" or symref.strip():
            continue
        names.append(name)
    return names


def _local_branches(repo: Path) -> list[str]:
    out = _git(["branch", "--format=%(refname:short)"], cwd=repo)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _default_branch(repo: Path) -> str:
    branches = _local_branches(repo)
    return "main" if "main" in branches else (branches[0] if branches else "main")


def refresh_bundles(root: Path) -> list[Path]:
    """Regenerate bundles from the working fixture repositories.

    Run this after changing a fixture, so the tracked bundle matches what runs actually use.
    """
    written: list[Path] = []
    bundles_dir = root / "fixtures" / "bundles"
    bundles_dir.mkdir(parents=True, exist_ok=True)
    for repo in sorted((root / "fixtures").iterdir()):
        if not (repo / ".git").exists():
            continue
        dirty = _git(["status", "--porcelain"], cwd=repo).strip()
        if dirty:
            raise FixtureError(
                f"fixture {repo.name} has uncommitted changes; commit them before "
                f"regenerating its bundle:\n{dirty}"
            )
        target = bundles_dir / f"{repo.name}.bundle"
        _git(["bundle", "create", str(target.resolve()), "--all"], cwd=repo)
        written.append(target)
    return written


def missing_fixtures(root: Path) -> list[str]:
    """Bundles with no materialised repository -- what `doctor` should complain about."""
    return [
        b.stem for b in bundle_paths(root) if not (root / "fixtures" / b.stem / ".git").exists()
    ]
