"""Run-level models: outcome, tool events, validation, metrics, environment, and the
top-level RunRecord that ties them together."""

from __future__ import annotations

import platform
import shutil
import subprocess
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from acb.models.usage import CostBreakdown, NormalizedUsage

RunOutcome = Literal["success", "failure", "timeout", "error", "setup_error", "skipped"]


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


class ToolEvent(BaseModel):
    """One tool call, reconstructed from the stream-json trajectory."""

    index: int
    tool_use_id: str | None = None
    name: str
    kind: str = Field(description="Normalised family: bash/read/write/search/git/mcp/...")
    input_digest: str = Field(default="", description="Stable hash of the normalised input.")
    input_summary: str = ""
    target_path: str | None = Field(default=None, description="File path, when applicable.")
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    is_error: bool = False
    error_summary: str | None = None
    is_repeat: bool = Field(default=False, description="Same tool + same normalised input seen.")
    parent_tool_use_id: str | None = Field(
        default=None, description="Set for calls made inside a subagent."
    )
    mcp_server: str | None = None


class ValidationResult(BaseModel):
    name: str
    kind: str
    command: str
    hidden: bool
    phase: Literal["baseline", "post"] = "post"
    passed: bool = False
    exit_code: int | None = None
    duration_ms: int = 0
    timed_out: bool = False
    stdout_tail: str = ""
    stderr_tail: str = ""
    tests_passed: int | None = None
    tests_failed: int | None = None
    tests_total: int | None = None
    weight: float = 1.0
    error: str | None = None


class ToolMetrics(BaseModel):
    total_calls: int = 0
    bash_calls: int = 0
    read_calls: int = 0
    write_calls: int = 0
    edit_calls: int = 0
    search_calls: int = 0
    git_calls: int = 0
    mcp_calls: int = 0
    web_calls: int = 0
    subagent_calls: int = 0
    other_calls: int = 0
    failed_calls: int = 0
    repeated_calls: int = 0
    repeated_reads: int = 0
    distinct_files_read: int = 0
    distinct_files_modified: int = 0
    sequence: list[str] = Field(default_factory=list)

    # MCP-specific quality signals (category `mcp`).
    mcp_servers_used: list[str] = Field(default_factory=list)
    mcp_expected_tools_used: list[str] = Field(default_factory=list)
    mcp_missing_tools: list[str] = Field(default_factory=list)
    mcp_forbidden_tools_used: list[str] = Field(default_factory=list)
    mcp_redundant_calls: int = 0

    @property
    def failed_call_rate(self) -> float:
        return self.failed_calls / self.total_calls if self.total_calls else 0.0

    def sequence_string(self, limit: int = 60) -> str:
        seq = self.sequence[:limit]
        s = " -> ".join(seq)
        if len(self.sequence) > limit:
            s += f" -> ... (+{len(self.sequence) - limit})"
        return s


class TimeMetrics(BaseModel):
    wall_clock_ms: int = 0
    api_ms: int | None = None
    ttft_ms: int | None = None
    time_to_first_edit_ms: int | None = None
    time_to_first_passing_validation_ms: int | None = None
    validation_ms: int = 0
    setup_ms: int = 0


class OutcomeMetrics(BaseModel):
    success: bool = False
    build_passed: bool | None = None
    tests_passed: bool | None = None
    hidden_tests_passed: bool | None = None
    lint_passed: bool | None = None
    typecheck_passed: bool | None = None
    test_pass_rate: float | None = None
    hidden_test_pass_rate: float | None = None
    regression_count: int = 0
    regressions: list[str] = Field(default_factory=list)
    files_changed: int = 0
    lines_added: int = 0
    lines_removed: int = 0
    touched_expected_files: int | None = Field(
        default=None, description="Advisory overlap with task.expected_files. Never a gate."
    )
    failure_reason: str | None = None


class ScoreBreakdown(BaseModel):
    functional_correctness: float | None = None
    code_quality: float | None = None
    agent_efficiency: float | None = None
    time: float | None = None
    cost: float | None = None
    weights: dict[str, float] = Field(default_factory=dict)
    composite: float = 0.0
    notes: list[str] = Field(default_factory=list)


class EnvironmentInfo(BaseModel):
    """Everything needed to explain, later, why two runs might differ."""

    os: str = ""
    os_version: str = ""
    arch: str = ""
    cpu_count: int | None = None
    ram_gb: float | None = None
    gpu: str | None = None
    python_version: str = ""
    claude_code_version: str | None = None
    framework_version: str = ""
    hostname: str = ""
    git_commit: str | None = Field(default=None, description="Commit of the framework repo.")
    fixture_commit: str | None = Field(default=None, description="Commit the agent started at.")
    mcp_fingerprint: str | None = None
    mcp_servers: list[dict[str, Any]] = Field(default_factory=list)
    tools_available: list[str] = Field(default_factory=list)
    permission_mode: str | None = None
    session_id: str | None = None
    model_reported: str | None = Field(
        default=None, description="Model the CLI reported, which may differ from the request."
    )
    notes: list[str] = Field(default_factory=list)

    @classmethod
    def collect(cls, framework_version: str) -> EnvironmentInfo:
        info = cls(
            os=platform.system(),
            os_version=platform.release(),
            arch=platform.machine(),
            python_version=platform.python_version(),
            framework_version=framework_version,
            hostname=platform.node(),
        )
        try:
            import os as _os

            info.cpu_count = _os.cpu_count()
        except Exception:  # pragma: no cover - defensive
            pass
        info.ram_gb = _detect_ram_gb()
        info.gpu = _detect_gpu()
        info.claude_code_version = _detect_claude_version()
        info.git_commit = _git_head(".")
        return info


def _detect_ram_gb() -> float | None:
    try:
        import os as _os

        if hasattr(_os, "sysconf") and "SC_PAGE_SIZE" in _os.sysconf_names:
            pages = _os.sysconf("SC_PHYS_PAGES")
            page = _os.sysconf("SC_PAGE_SIZE")
            return round(pages * page / (1024**3), 2)
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
        )
        if out.returncode == 0:
            return round(int(out.stdout.strip()) / (1024**3), 2)
    except Exception:
        pass
    return None


def _detect_gpu() -> str | None:
    if shutil.which("nvidia-smi"):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if out.returncode == 0 and out.stdout.strip():
                names = [n.strip() for n in out.stdout.strip().splitlines() if n.strip()]
                return f"{len(names)}x {names[0]}" if names else None
        except Exception:
            return None
    return None


def _detect_claude_version() -> str | None:
    if not shutil.which("claude"):
        return None
    try:
        out = subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=20)
        return out.stdout.strip() or None
    except Exception:
        return None


def _git_head(path: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", path, "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
        return out.stdout.strip() if out.returncode == 0 else None
    except Exception:
        return None


class RunRecord(BaseModel):
    """The complete, self-contained record of one benchmark run."""

    run_id: str
    task_id: str
    task_version: int = 1
    task_category: str = ""
    task_difficulty: str = ""
    target_id: str
    model: str
    model_family: str | None = None
    provider: str
    deployment_type: str = "api"
    repetition: int = 1
    batch_id: str | None = None

    started_at: str = Field(default_factory=utcnow)
    finished_at: str | None = None
    outcome: RunOutcome = "error"
    timed_out: bool = False
    error: str | None = None
    terminal_reason: str | None = None
    agent_exit_code: int | None = None
    num_turns: int = 0
    permission_denials: int = 0

    usage: NormalizedUsage = Field(default_factory=NormalizedUsage)
    cost: CostBreakdown = Field(default_factory=CostBreakdown)
    reported_cost_usd: float | None = Field(
        default=None,
        description="Claude Code's own total_cost_usd. First-party price table only, so it "
        "is a cross-check, NEVER used for comparison across targets.",
    )

    tools: ToolMetrics = Field(default_factory=ToolMetrics)
    time: TimeMetrics = Field(default_factory=TimeMetrics)
    result: OutcomeMetrics = Field(default_factory=OutcomeMetrics)
    score: ScoreBreakdown = Field(default_factory=ScoreBreakdown)
    validations: list[ValidationResult] = Field(default_factory=list)
    environment: EnvironmentInfo = Field(default_factory=EnvironmentInfo)
    pricing_snapshot: dict[str, Any] = Field(default_factory=dict)
    artifacts_dir: str = ""
