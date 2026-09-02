"""Outcome evaluation and composite scoring.

Two principles from the design doc:

* **Cost and time are normalised within a comparison**, not against absolute constants, so
  the composite stays meaningful as prices and hardware change.
* **A composite is never the whole story.** Every reported score carries its raw
  sub-scores and the weights used, and the reporting layer always shows raw metrics too.

Dimensions a task does not configure (no linter, unknown cost) are dropped and the
remaining weights re-normalised, so a task is never penalised for absent tooling.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from acb.models.run import (
    OutcomeMetrics,
    ScoreBreakdown,
    TimeMetrics,
    ToolMetrics,
    ValidationResult,
)
from acb.models.task import TaskSpec


class ScoringWeights(BaseModel):
    functional_correctness: float = 0.50
    code_quality: float = 0.15
    agent_efficiency: float = 0.15
    time: float = 0.10
    cost: float = 0.10

    # Efficiency shaping. A run that uses `tool_call_budget` calls scores 1.0; usage is
    # penalised smoothly beyond that rather than at a cliff edge.
    default_tool_call_budget: int = 40
    failed_call_penalty: float = 2.0
    repeated_read_penalty: float = 1.0

    def as_dict(self) -> dict[str, float]:
        return {
            "functional_correctness": self.functional_correctness,
            "code_quality": self.code_quality,
            "agent_efficiency": self.agent_efficiency,
            "time": self.time,
            "cost": self.cost,
        }


class CostContext(BaseModel):
    """Cost normalisation window, supplied by the comparison being rendered."""

    min_cost: float | None = None
    max_cost: float | None = None
    run_cost: float | None = None


def evaluate_outcome(
    task: TaskSpec,
    validations: list[ValidationResult],
    regression_count: int,
    regressions: list[str],
    timed_out: bool,
    agent_error: str | None = None,
) -> OutcomeMetrics:
    """Apply the task's success criteria to its validation results."""
    m = OutcomeMetrics(regression_count=regression_count, regressions=regressions)
    post = [v for v in validations if v.phase == "post"]

    def agg(items: list[ValidationResult]) -> bool | None:
        return all(v.passed for v in items) if items else None

    visible_tests = [v for v in post if v.kind == "test" and not v.hidden]
    hidden_tests = [v for v in post if v.kind == "test" and v.hidden]

    m.tests_passed = agg(visible_tests)
    m.hidden_tests_passed = agg(hidden_tests)
    m.build_passed = agg([v for v in post if v.kind == "build"])
    m.lint_passed = agg([v for v in post if v.kind == "lint"])
    m.typecheck_passed = agg([v for v in post if v.kind == "typecheck"])
    m.test_pass_rate = _pass_rate(visible_tests)
    m.hidden_test_pass_rate = _pass_rate(hidden_tests)

    reasons: list[str] = []
    if timed_out:
        reasons.append("timed out")
    if agent_error:
        reasons.append(f"agent error: {agent_error}")

    c = task.success
    if c.require_tests_pass and m.tests_passed is not True:
        reasons.append("visible tests did not pass")
    if c.require_hidden_tests_pass and m.hidden_tests_passed is not True:
        reasons.append("hidden tests did not pass")
    if c.require_clean_build and m.build_passed is False:
        reasons.append("build failed")
    if c.require_lint_pass and m.lint_passed is not True:
        reasons.append("lint did not pass")
    if c.require_typecheck_pass and m.typecheck_passed is not True:
        reasons.append("type check did not pass")
    if c.require_no_regressions and regression_count > 0:
        reasons.append(f"{regression_count} regression(s): {', '.join(regressions[:5])}")

    m.success = not reasons
    m.failure_reason = "; ".join(reasons) if reasons else None
    return m


def _pass_rate(items: list[ValidationResult]) -> float | None:
    """Test pass rate, preferring parsed per-test counts over per-command pass/fail."""
    if not items:
        return None
    counted = [v for v in items if v.tests_total]
    if counted:
        total = sum(v.tests_total or 0 for v in counted)
        passed = sum(v.tests_passed or 0 for v in counted)
        return passed / total if total else None
    weight = sum(v.weight for v in items)
    return sum(v.weight for v in items if v.passed) / weight if weight else None


def score_functional_correctness(outcome: OutcomeMetrics, task: TaskSpec) -> float:
    """Weighted blend of test signals, penalised for regressions."""
    parts: list[tuple[float, float]] = []  # (value, weight)
    if outcome.test_pass_rate is not None:
        parts.append((outcome.test_pass_rate, 2.0))
    if outcome.hidden_test_pass_rate is not None:
        parts.append((outcome.hidden_test_pass_rate, 2.0))
    if outcome.build_passed is not None:
        parts.append((1.0 if outcome.build_passed else 0.0, 1.0))
    if not parts:
        return 1.0 if outcome.success else 0.0

    base = sum(v * w for v, w in parts) / sum(w for _, w in parts)
    if outcome.regression_count:
        # Regressions are severe: half the score, then a further linear penalty.
        base *= max(0.0, 0.5 - 0.1 * (outcome.regression_count - 1))
    return max(0.0, min(1.0, base))


def score_code_quality(outcome: OutcomeMetrics) -> float | None:
    signals = [s for s in (outcome.lint_passed, outcome.typecheck_passed) if s is not None]
    if not signals:
        return None  # Not measurable for this task; weight is redistributed.
    return sum(1.0 for s in signals if s) / len(signals)


def score_agent_efficiency(
    tools: ToolMetrics, task: TaskSpec, weights: ScoringWeights
) -> float | None:
    if tools.total_calls == 0:
        return None
    budget = task.tool_call_budget or weights.default_tool_call_budget

    # Volume: 1.0 at or under budget, decaying gracefully beyond it.
    volume = 1.0 if tools.total_calls <= budget else budget / tools.total_calls

    # Waste: failed calls and re-reading the same file both signal poor agentic control.
    waste = (
        weights.failed_call_penalty * tools.failed_calls
        + weights.repeated_read_penalty * tools.repeated_reads
    ) / max(tools.total_calls, 1)
    return max(0.0, min(1.0, volume * (1.0 - min(waste, 1.0))))


def score_time(time_metrics: TimeMetrics, task: TaskSpec) -> float:
    """1.0 for instant, 0.0 at the task timeout. Linear in between."""
    limit_ms = task.timeout_seconds * 1000
    if limit_ms <= 0:
        return 0.0
    return max(0.0, min(1.0, 1.0 - time_metrics.wall_clock_ms / limit_ms))


def score_cost(ctx: CostContext) -> float | None:
    """Normalise cost across the comparison set: cheapest scores 1.0, dearest 0.0."""
    if ctx.run_cost is None or ctx.min_cost is None or ctx.max_cost is None:
        return None
    if ctx.max_cost <= ctx.min_cost:
        return 1.0
    return max(0.0, min(1.0, 1.0 - (ctx.run_cost - ctx.min_cost) / (ctx.max_cost - ctx.min_cost)))


def compute_score(
    task: TaskSpec,
    outcome: OutcomeMetrics,
    tools: ToolMetrics,
    time_metrics: TimeMetrics,
    cost_ctx: CostContext | None = None,
    weights: ScoringWeights | None = None,
) -> ScoreBreakdown:
    w = weights or ScoringWeights()
    breakdown = ScoreBreakdown(weights=w.as_dict())

    breakdown.functional_correctness = score_functional_correctness(outcome, task)
    breakdown.code_quality = score_code_quality(outcome)
    breakdown.agent_efficiency = score_agent_efficiency(tools, task, w)
    breakdown.time = score_time(time_metrics, task)
    breakdown.cost = score_cost(cost_ctx or CostContext())

    dimensions = {
        "functional_correctness": breakdown.functional_correctness,
        "code_quality": breakdown.code_quality,
        "agent_efficiency": breakdown.agent_efficiency,
        "time": breakdown.time,
        "cost": breakdown.cost,
    }
    available = {k: v for k, v in dimensions.items() if v is not None}
    dropped = sorted(set(dimensions) - set(available))
    if dropped:
        breakdown.notes.append(
            "weights re-normalised; not measurable for this run: " + ", ".join(dropped)
        )

    total_weight = sum(w.as_dict()[k] for k in available)
    breakdown.composite = (
        sum(v * w.as_dict()[k] for k, v in available.items()) / total_weight
        if total_weight > 0
        else 0.0
    )
    return breakdown


class ScoringConfig(BaseModel):
    weights: ScoringWeights = Field(default_factory=ScoringWeights)

    @classmethod
    def load(cls, path) -> ScoringConfig:
        from pathlib import Path as _Path

        import yaml

        p = _Path(path)
        if not p.exists():
            return cls()
        return cls.model_validate(yaml.safe_load(p.read_text()) or {})
