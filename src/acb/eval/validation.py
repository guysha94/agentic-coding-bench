"""Objective validation.

Validation runs twice with identical commands: once at **baseline** (clean workspace,
before the agent) and once **post** (after the agent). Comparing the two is what makes
"fixed it without breaking anything" objectively checkable -- a check that was already
failing before the agent touched anything is not a regression, and a check that flipped
from pass to fail is.

Hidden checks are delivered into the workspace only during the post phase, so the agent
can never read or game them.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

from acb.models.run import ValidationResult
from acb.models.task import TaskSpec, ValidationCommand
from acb.runner.env import ENV_ALLOWLIST, redact

TAIL_CHARS = 4000

# Test-count parsers, most specific first. Best-effort: a command whose output we cannot
# parse still yields a pass/fail from its exit code.
_PATTERNS = [
    # pytest: "5 passed, 2 failed" / "1 failed, 3 passed in 0.12s"
    (
        re.compile(r"(?:(?P<failed>\d+) failed)?[,\s]*(?:(?P<passed>\d+) passed)", re.IGNORECASE),
        "pytest",
    ),
    # jest/vitest: "Tests: 1 failed, 5 passed, 6 total"
    (
        re.compile(
            r"Tests?:\s+(?:(?P<failed>\d+) failed,\s*)?(?:\d+ skipped,\s*)?"
            r"(?P<passed>\d+) passed,\s*(?P<total>\d+) total",
            re.IGNORECASE,
        ),
        "jest",
    ),
    # go test: counts lines
    (re.compile(r"^(?P<ok>ok|FAIL)\s+\S+", re.MULTILINE), "go"),
]


def parse_test_counts(output: str) -> tuple[int | None, int | None, int | None]:
    """Best-effort (passed, failed, total) from test-runner output."""
    for pattern, flavour in _PATTERNS:
        if flavour == "go":
            oks = len(re.findall(r"^ok\s+\S+", output, re.MULTILINE))
            fails = len(re.findall(r"^FAIL\s+\S+", output, re.MULTILINE))
            if oks or fails:
                return oks, fails, oks + fails
            continue
        match = pattern.search(output)
        if not match:
            continue
        groups = match.groupdict()
        passed = int(groups["passed"]) if groups.get("passed") else None
        failed = int(groups["failed"]) if groups.get("failed") else 0
        if passed is None:
            continue
        total = int(groups["total"]) if groups.get("total") else passed + (failed or 0)
        return passed, failed, total
    return None, None, None


def _validation_env() -> dict[str, str]:
    """Minimal, deterministic environment for validation commands.

    Validation must not depend on whichever interpreter happens to be first on the
    operator's PATH -- that is how a task silently "fails" on one machine and passes on
    another. The harness therefore injects `ACB_PYTHON`, pointing at the interpreter the
    framework itself is running under (which has pytest and ruff installed). Task files
    reference `$ACB_PYTHON` instead of a bare `python`.
    """
    import os
    import sys

    src = dict(os.environ)
    env = {k: src[k] for k in ENV_ALLOWLIST if k in src}
    env["ACB_VALIDATION"] = "1"
    env["ACB_PYTHON"] = os.environ.get("ACB_PYTHON") or sys.executable
    # Put the harness interpreter's bin dir first, so a bare `pytest`/`ruff` also resolves
    # to the toolchain we control rather than an arbitrary ambient one.
    bin_dir = str(Path(sys.executable).parent)
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    # Keep test output deterministic and parseable.
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["NO_COLOR"] = "1"
    env["CI"] = "1"
    return env


def _provide_files(check: ValidationCommand, workspace: Path, task_dir: Path) -> str | None:
    """Copy a check's files (e.g. hidden tests) into the workspace. Returns an error msg."""
    for dest_rel, src_rel in check.provides_files.items():
        src = (task_dir / src_rel).resolve()
        dest = (workspace / dest_rel).resolve()
        if not src.exists():
            return f"provided file not found: {src_rel}"
        if not str(dest).startswith(str(workspace.resolve())):
            return f"provided file destination escapes the workspace: {dest_rel}"
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
    return None


def run_check(
    check: ValidationCommand,
    workspace: Path,
    task_dir: Path,
    phase: str = "post",
    secrets: list[str] | None = None,
) -> ValidationResult:
    result = ValidationResult(
        name=check.name,
        kind=check.kind,
        command=check.command,
        hidden=check.hidden,
        phase=phase,  # type: ignore[arg-type]
        weight=check.weight,
    )

    err = _provide_files(check, workspace, task_dir)
    if err:
        result.error = err
        result.passed = False
        return result

    started = time.monotonic()
    try:
        proc = subprocess.run(
            check.command,
            shell=True,
            cwd=str(workspace),
            env=_validation_env(),
            capture_output=True,
            text=True,
            timeout=check.timeout_seconds,
        )
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        code = None
        result.timed_out = True
    except OSError as exc:
        result.error = f"failed to execute check: {exc}"
        result.duration_ms = int((time.monotonic() - started) * 1000)
        return result

    result.duration_ms = int((time.monotonic() - started) * 1000)
    result.exit_code = code
    result.stdout_tail = redact(stdout[-TAIL_CHARS:], secrets)
    result.stderr_tail = redact(stderr[-TAIL_CHARS:], secrets)

    succeeded = (code == 0) and not result.timed_out
    result.passed = (not succeeded) if check.expect_failure else succeeded

    if check.kind == "test":
        passed, failed, total = parse_test_counts(stdout + "\n" + stderr)
        result.tests_passed, result.tests_failed, result.tests_total = passed, failed, total
    return result


def run_validation(
    task: TaskSpec,
    workspace: Path,
    task_dir: Path,
    phase: str = "post",
    secrets: list[str] | None = None,
) -> list[ValidationResult]:
    """Run the checks appropriate to a phase.

    Baseline runs only visible checks -- hidden test files must not exist in the workspace
    while the agent is working.
    """
    checks = task.visible_validation() if phase == "baseline" else task.validation
    return [run_check(c, workspace, task_dir, phase, secrets) for c in checks]


def detect_regressions(
    baseline: list[ValidationResult], post: list[ValidationResult]
) -> tuple[int, list[str]]:
    """Checks that passed before the agent ran and fail after it."""
    before = {r.name: r.passed for r in baseline if r.phase == "baseline"}
    regressions = [
        r.name for r in post if r.phase == "post" and before.get(r.name) is True and not r.passed
    ]
    return len(regressions), regressions
