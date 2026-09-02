"""Framework configuration and path resolution."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from acb.eval.scoring import ScoringWeights
from acb.models.target import TargetConfig, load_targets
from acb.models.task import SuiteSpec, TaskSpec, load_suites, load_tasks


def find_repo_root(start: Path | None = None) -> Path:
    """Nearest ancestor containing a `tasks/` directory, else the env override, else cwd."""
    if env := os.environ.get("ACB_ROOT"):
        return Path(env).resolve()
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / "tasks").is_dir() and (candidate / "config").is_dir():
            return candidate
    return current


class Paths(BaseModel):
    root: Path
    tasks_dir: Path
    suites_dir: Path
    config_dir: Path
    targets_file: Path
    scoring_file: Path
    runs_dir: Path
    reports_dir: Path
    db_path: Path
    fixtures_dir: Path

    @classmethod
    def resolve(cls, root: Path | None = None) -> Paths:
        r = (root or find_repo_root()).resolve()
        return cls(
            root=r,
            tasks_dir=r / "tasks",
            suites_dir=r / "config" / "suites",
            config_dir=r / "config",
            targets_file=r / "config" / "targets.yaml",
            scoring_file=r / "config" / "scoring.yaml",
            runs_dir=r / "runs",
            reports_dir=r / "reports",
            db_path=r / "acb.sqlite3",
            fixtures_dir=r / "fixtures",
        )


class Workspace(BaseModel):
    """Everything loaded from disk: tasks, suites, targets, weights."""

    paths: Paths
    tasks: dict[str, TaskSpec] = Field(default_factory=dict)
    suites: dict[str, SuiteSpec] = Field(default_factory=dict)
    targets: dict[str, TargetConfig] = Field(default_factory=dict)
    weights: ScoringWeights = Field(default_factory=ScoringWeights)

    @classmethod
    def load(cls, root: Path | None = None) -> Workspace:
        paths = Paths.resolve(root)
        tasks = load_tasks(paths.tasks_dir) if paths.tasks_dir.exists() else {}
        suites = load_suites(paths.suites_dir)
        targets = load_targets(paths.targets_file) if paths.targets_file.exists() else {}

        weights = ScoringWeights()
        if paths.scoring_file.exists():
            import yaml

            data = yaml.safe_load(paths.scoring_file.read_text()) or {}
            weights = ScoringWeights.model_validate(data.get("weights", data))
        return cls(paths=paths, tasks=tasks, suites=suites, targets=targets, weights=weights)

    def resolve_tasks(self, task_ids: list[str] | None, suite_id: str | None) -> list[TaskSpec]:
        if suite_id:
            suite = self.suites.get(suite_id)
            if suite is None:
                raise KeyError(
                    f"unknown suite {suite_id!r}; available: {', '.join(sorted(self.suites))}"
                )
            missing = [t for t in suite.tasks if t not in self.tasks]
            if missing:
                raise KeyError(f"suite {suite_id!r} references unknown tasks: {', '.join(missing)}")
            return [self.tasks[t] for t in suite.tasks]
        if task_ids:
            missing = [t for t in task_ids if t not in self.tasks]
            if missing:
                raise KeyError(f"unknown task(s): {', '.join(missing)}")
            return [self.tasks[t] for t in task_ids]
        return list(self.tasks.values())

    def resolve_targets(self, target_ids: list[str] | None) -> list[TargetConfig]:
        if not target_ids:
            return [t for t in self.targets.values() if t.enabled]
        missing = [t for t in target_ids if t not in self.targets]
        if missing:
            raise KeyError(
                f"unknown target(s): {', '.join(missing)}; "
                f"available: {', '.join(sorted(self.targets))}"
            )
        return [self.targets[t] for t in target_ids]

    def task_dir(self, task: TaskSpec) -> Path:
        """Directory a task's files (e.g. hidden tests) are resolved against."""
        for p in self.paths.tasks_dir.rglob("*.yaml"):
            try:
                import yaml

                data = yaml.safe_load(p.read_text())
            except Exception:
                continue
            if isinstance(data, dict) and data.get("id") == task.id:
                return p.parent
        return self.paths.tasks_dir
