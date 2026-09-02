"""Task and suite parsing, including the real task files shipped with the framework."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from acb.config import Workspace
from acb.models.task import KNOWN_CATEGORIES, SuiteSpec, TaskSpec, load_task, load_tasks

ROOT = Path(__file__).resolve().parent.parent


def test_minimal_task_parses(tmp_path: Path):
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": "t1",
                "name": "T",
                "category": "bug_fix",
                "repository": {"path": "./fixtures/x"},
                "task": {"prompt": "do it"},
                "validation": [{"name": "t", "kind": "test", "command": "pytest"}],
            }
        )
    )
    task = load_task(path)
    assert task.id == "t1"
    assert task.timeout_seconds == 20 * 60
    assert task.difficulty == "medium"


def test_require_tests_pass_without_test_command_is_rejected(tmp_path: Path):
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": "t1",
                "name": "T",
                "category": "bug_fix",
                "repository": {"path": "./x"},
                "task": {"prompt": "p"},
                "validation": [{"name": "l", "kind": "lint", "command": "ruff check ."}],
            }
        )
    )
    with pytest.raises(ValidationError, match="no validation command of kind 'test'"):
        load_task(path)


def test_hidden_gate_is_softened_when_no_hidden_checks_exist(tmp_path: Path):
    """A task with no hidden tests must not be gated on a criterion it cannot evaluate."""
    path = tmp_path / "t.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "id": "t1",
                "name": "T",
                "category": "bug_fix",
                "repository": {"path": "./x"},
                "task": {"prompt": "p"},
                "validation": [{"name": "t", "kind": "test", "command": "pytest"}],
                "success": {"require_hidden_tests_pass": True},
            }
        )
    )
    assert load_task(path).success.require_hidden_tests_pass is False


def test_negative_weight_rejected():
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(
            {
                "id": "t",
                "name": "T",
                "category": "bug_fix",
                "repository": {"path": "./x"},
                "task": {"prompt": "p"},
                "validation": [
                    {"name": "t", "kind": "test", "command": "pytest", "weight": 0}
                ],
            }
        )


def test_duplicate_task_ids_rejected(tmp_path: Path):
    for name in ("a.yaml", "b.yaml"):
        (tmp_path / name).write_text(
            yaml.safe_dump(
                {
                    "id": "same",
                    "name": "T",
                    "category": "bug_fix",
                    "repository": {"path": "./x"},
                    "task": {"prompt": "p"},
                    "validation": [{"name": "t", "kind": "test", "command": "pytest"}],
                }
            )
        )
    with pytest.raises(ValueError, match="duplicate task id"):
        load_tasks(tmp_path)


def test_visible_and_hidden_partition(sample_task):
    assert len(sample_task.visible_validation()) == 1
    assert sample_task.hidden_validation() == []


# ------------------------------------------------------- the real shipped task files --
def test_shipped_tasks_all_parse():
    tasks = load_tasks(ROOT / "tasks")
    assert len(tasks) >= 8, "expected the proof-of-concept suite to be present"
    for task in tasks.values():
        assert task.category in KNOWN_CATEGORIES, f"{task.id} has unknown category"
        assert task.task.prompt.strip(), f"{task.id} has an empty prompt"
        assert task.validation, f"{task.id} has no validation commands"


def test_shipped_tasks_cover_the_required_categories():
    """The brief requires at least these categories in the initial suite."""
    tasks = load_tasks(ROOT / "tasks")
    covered = {t.category for t in tasks.values()}
    for required in (
        "bug_fix", "feature", "refactor", "repo_understanding", "devops", "multi_step"
    ):
        assert required in covered, f"no task covers the {required!r} category"


def test_shipped_tasks_have_at_least_one_hidden_check():
    tasks = load_tasks(ROOT / "tasks")
    assert any(t.hidden_validation() for t in tasks.values())


def test_hidden_provided_files_exist():
    ws = Workspace.load(ROOT)
    for task in ws.tasks.values():
        task_dir = ws.task_dir(task)
        for check in task.validation:
            for src in check.provides_files.values():
                assert (task_dir / src).exists(), f"{task.id}: missing provided file {src}"


def test_shipped_suites_reference_known_tasks():
    ws = Workspace.load(ROOT)
    assert ws.suites, "expected shipped suites"
    for suite in ws.suites.values():
        for task_id in suite.tasks:
            assert task_id in ws.tasks, f"suite {suite.id} references unknown task {task_id}"


def test_suite_roundtrip(tmp_path: Path):
    path = tmp_path / "s.yaml"
    path.write_text(yaml.safe_dump({"id": "s", "name": "S", "tasks": ["a", "b"]}))
    assert SuiteSpec.load(path).tasks == ["a", "b"]


def test_expected_files_are_advisory_not_a_gate():
    """Different correct solutions exist, so expected_files must never gate success."""
    task = load_task(ROOT / "tasks" / "payments-bug-002.yaml")
    assert task.expected_files
    field = TaskSpec.model_fields["expected_files"]
    assert "ADVISORY" in (field.description or "")
