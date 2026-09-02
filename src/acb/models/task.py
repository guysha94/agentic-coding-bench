"""Benchmark task specification.

A task file describes *what work to do and how to check it*. It never names a model,
provider, endpoint or price -- that is the target's job (see `acb.models.target`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

# Categories from the design doc. Kept as a plain str (not an Enum) so new categories can be
# added in YAML without a code change; `KNOWN_CATEGORIES` drives reporting order only.
KNOWN_CATEGORIES = (
    "bug_fix",
    "feature",
    "refactor",
    "repo_understanding",
    "large_context",
    "debugging",
    "devops",
    "mcp",
    "multi_step",
)

Difficulty = Literal["easy", "medium", "hard"]


class RepositorySpec(BaseModel):
    """Where the agent works, and from which exact commit."""

    path: str = Field(description="Path to the fixture repo, relative to the repo root.")
    base_commit: str | None = Field(
        default=None,
        description="Exact commit to check out. None means 'current HEAD of the fixture', "
        "which is resolved and recorded at run time so the run stays reproducible.",
    )
    isolation: Literal["worktree", "clone_temp", "container"] = "worktree"
    setup_commands: list[str] = Field(
        default_factory=list,
        description="Commands run in the fresh workspace before the agent starts "
        "(dependency install, fixture seeding). Failures abort the run as a setup error.",
    )


class TaskPrompt(BaseModel):
    prompt: str = Field(description="Byte-identical instruction handed to every target.")
    context_files: list[str] = Field(
        default_factory=list,
        description="Files whose contents are appended to the prompt (logs, stack traces).",
    )


class ValidationCommand(BaseModel):
    """One objective check. Exit code 0 == pass unless `expect_failure` is set."""

    name: str
    command: str
    kind: Literal["test", "build", "lint", "typecheck", "custom"] = "test"
    hidden: bool = Field(
        default=False,
        description="Hidden checks are copied into the workspace only *after* the agent "
        "finishes, so the agent cannot read or game them.",
    )
    timeout_seconds: int = 600
    weight: float = 1.0
    expect_failure: bool = False
    # Files (relative to the task dir) copied into the workspace before this command runs.
    # This is how hidden tests are delivered without ever exposing them to the agent.
    provides_files: dict[str, str] = Field(default_factory=dict)

    @field_validator("weight")
    @classmethod
    def _positive_weight(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("weight must be > 0")
        return v


class SuccessCriteria(BaseModel):
    require_clean_build: bool = True
    require_tests_pass: bool = True
    require_hidden_tests_pass: bool = True
    require_no_regressions: bool = True
    require_lint_pass: bool = False
    require_typecheck_pass: bool = False
    min_score: float | None = Field(
        default=None, description="Optional floor on the composite score."
    )


class McpRequirement(BaseModel):
    """MCP expectations for category `mcp`, used for both setup and grading."""

    config_file: str | None = Field(
        default=None, description="MCP config JSON passed via --mcp-config, relative to repo root."
    )
    required_servers: list[str] = Field(default_factory=list)
    expected_tools: list[str] = Field(
        default_factory=list, description="Tools a correct solution is expected to call."
    )
    forbidden_tools: list[str] = Field(default_factory=list)
    max_calls: int | None = Field(
        default=None, description="Above this, MCP usage is judged wasteful."
    )


class TaskSpec(BaseModel):
    id: str
    name: str
    category: str
    difficulty: Difficulty = "medium"
    version: int = Field(default=1, description="Bump when task semantics change.")

    repository: RepositorySpec
    task: TaskPrompt
    timeout_minutes: int = 20

    validation: list[ValidationCommand] = Field(default_factory=list)
    success: SuccessCriteria = Field(default_factory=SuccessCriteria)

    # --- optional metadata -------------------------------------------------------------
    language: str | None = None
    framework: str | None = None
    repository_size_loc: int | None = None
    expected_complexity: str | None = None
    required_tools: list[str] | None = Field(
        default=None,
        description="Tool allowlist for this task. None => the suite/runner default.",
    )
    mcp: McpRequirement | None = None
    expected_files: list[str] = Field(
        default_factory=list,
        description="Files a typical solution touches. ADVISORY ONLY -- reported as a hint, "
        "never used as a success gate, because different correct solutions exist.",
    )
    expected_solution: str | None = Field(
        default=None, description="Prose description of the conceptual solution, for reviewers."
    )
    tool_call_budget: int | None = Field(
        default=None, description="Reference tool-call count for the efficiency score."
    )
    requires_network: bool = False
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_gates_have_commands(self) -> TaskSpec:
        kinds = {v.kind for v in self.validation}
        if self.success.require_tests_pass and "test" not in kinds:
            raise ValueError(
                f"task {self.id}: require_tests_pass is set but no validation command of "
                "kind 'test' is defined"
            )
        if self.success.require_hidden_tests_pass and not any(v.hidden for v in self.validation):
            # Not an error: a task may legitimately have no hidden tests. Soften the gate so
            # scoring does not silently pass a criterion that can never be evaluated.
            self.success.require_hidden_tests_pass = False
        return self

    @property
    def timeout_seconds(self) -> int:
        return self.timeout_minutes * 60

    def visible_validation(self) -> list[ValidationCommand]:
        return [v for v in self.validation if not v.hidden]

    def hidden_validation(self) -> list[ValidationCommand]:
        return [v for v in self.validation if v.hidden]


def load_task(path: Path) -> TaskSpec:
    data: dict[str, Any] = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: task file must be a YAML mapping")
    return TaskSpec.model_validate(data)


def load_tasks(directory: Path) -> dict[str, TaskSpec]:
    tasks: dict[str, TaskSpec] = {}
    for p in sorted(directory.rglob("*.yaml")):
        if p.name.startswith("_") or "suites" in p.parts:
            continue
        spec = load_task(p)
        if spec.id in tasks:
            raise ValueError(f"duplicate task id {spec.id!r} ({p})")
        tasks[spec.id] = spec
    return tasks


class SuiteSpec(BaseModel):
    id: str
    name: str
    description: str = ""
    tasks: list[str] = Field(default_factory=list, description="Task ids, in execution order.")

    @classmethod
    def load(cls, path: Path) -> SuiteSpec:
        return cls.model_validate(yaml.safe_load(path.read_text()))


def load_suites(directory: Path) -> dict[str, SuiteSpec]:
    suites: dict[str, SuiteSpec] = {}
    if not directory.exists():
        return suites
    for p in sorted(directory.glob("*.yaml")):
        s = SuiteSpec.load(p)
        suites[s.id] = s
    return suites
