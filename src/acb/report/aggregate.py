"""Aggregation and statistics across runs.

Reports mean, median, stdev, p50/p90, success rate with a **Wilson score interval** (which
stays honest at the small N this benchmark realistically has, unlike a naive proportion),
and the derived efficiency metrics that make targets comparable.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any

from pydantic import BaseModel, Field

from acb.models.run import RunRecord


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval. Returns (0, 1) for n = 0."""
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    denom = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile; q in [0, 1]."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return ordered[int(pos)]
    return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)


class Stats(BaseModel):
    n: int = 0
    mean: float | None = None
    median: float | None = None
    stdev: float | None = None
    p50: float | None = None
    p90: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    total: float | None = None

    @classmethod
    def of(cls, values: list[float]) -> Stats:
        clean = [v for v in values if v is not None]
        if not clean:
            return cls()
        return cls(
            n=len(clean),
            mean=statistics.fmean(clean),
            median=statistics.median(clean),
            stdev=statistics.stdev(clean) if len(clean) > 1 else 0.0,
            p50=percentile(clean, 0.5),
            p90=percentile(clean, 0.9),
            minimum=min(clean),
            maximum=max(clean),
            total=sum(clean),
        )


class TargetSummary(BaseModel):
    target_id: str
    display_name: str = ""
    model: str = ""
    model_family: str | None = None
    provider: str = ""
    deployment_type: str = ""
    pricing_type: str = "unknown"
    currency: str = "USD"

    runs: int = 0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    errors: int = 0
    success_rate: float = 0.0
    success_rate_ci: tuple[float, float] = (0.0, 1.0)

    duration: Stats = Field(default_factory=Stats)
    cost: Stats = Field(default_factory=Stats)
    tool_calls: Stats = Field(default_factory=Stats)
    score: Stats = Field(default_factory=Stats)
    turns: Stats = Field(default_factory=Stats)

    total_cost: float | None = None
    cost_per_successful_task: float | None = None
    cost_per_failed_task: float | None = None
    tool_calls_per_successful_task: float | None = None
    wall_time_per_successful_task_s: float | None = None
    tokens_per_successful_task: float | None = None
    mcp_calls_per_successful_task: float | None = None
    failed_calls_per_run: float = 0.0
    repeated_reads_per_run: float = 0.0

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    input_token_cost: float = 0.0
    output_token_cost: float = 0.0
    cache_savings: float | None = None

    cost_incomplete: bool = False
    cost_is_estimate: bool = False
    warnings: list[str] = Field(default_factory=list)

    by_category: dict[str, CategorySummary] = Field(default_factory=dict)


class CategorySummary(BaseModel):
    category: str
    runs: int = 0
    successes: int = 0
    success_rate: float = 0.0
    success_rate_ci: tuple[float, float] = (0.0, 1.0)
    mean_score: float | None = None
    mean_cost: float | None = None
    cost_per_successful_task: float | None = None
    mean_duration_s: float | None = None
    mean_tool_calls: float | None = None


def _cost_values(runs: list[RunRecord]) -> list[float]:
    return [r.cost.total_cost for r in runs if r.cost.total_cost is not None]


def summarize_target(target_id: str, runs: list[RunRecord]) -> TargetSummary:
    s = TargetSummary(target_id=target_id)
    if not runs:
        return s

    first = runs[0]
    s.model = first.model
    s.model_family = first.model_family
    s.provider = first.provider
    s.deployment_type = first.deployment_type
    s.display_name = f"{first.model_family or first.model} / {first.provider}"
    s.pricing_type = first.cost.pricing_type
    s.currency = first.cost.currency

    s.runs = len(runs)
    successful = [r for r in runs if r.result.success]
    s.successes = len(successful)
    s.failures = sum(1 for r in runs if r.outcome == "failure")
    s.timeouts = sum(1 for r in runs if r.outcome == "timeout")
    s.errors = sum(1 for r in runs if r.outcome in ("error", "setup_error"))
    s.success_rate = s.successes / s.runs
    s.success_rate_ci = wilson_interval(s.successes, s.runs)

    s.duration = Stats.of([r.time.wall_clock_ms / 1000.0 for r in runs])
    s.tool_calls = Stats.of([float(r.tools.total_calls) for r in runs])
    s.score = Stats.of([r.score.composite for r in runs])
    s.turns = Stats.of([float(r.num_turns) for r in runs])

    costs = _cost_values(runs)
    s.cost = Stats.of(costs)
    s.total_cost = sum(costs) if costs else None
    s.cost_incomplete = any(r.cost.incomplete for r in runs)
    s.cost_is_estimate = any(r.cost.is_estimate for r in runs)
    for r in runs:
        s.warnings.extend(w for w in r.cost.warnings if w not in s.warnings)

    # Derived efficiency. These are the decision-grade numbers: a cheap model that fails
    # often can be more expensive per *successful* task than an expensive one.
    if s.successes:
        if s.total_cost is not None:
            s.cost_per_successful_task = s.total_cost / s.successes
        s.tool_calls_per_successful_task = sum(r.tools.total_calls for r in runs) / s.successes
        s.wall_time_per_successful_task_s = (
            sum(r.time.wall_clock_ms for r in runs) / 1000.0 / s.successes
        )
        s.tokens_per_successful_task = sum(r.usage.total_tokens for r in runs) / s.successes
        s.mcp_calls_per_successful_task = sum(r.tools.mcp_calls for r in runs) / s.successes

    failed_runs = [r for r in runs if not r.result.success]
    failed_costs = _cost_values(failed_runs)
    if failed_runs and failed_costs:
        s.cost_per_failed_task = sum(failed_costs) / len(failed_runs)

    s.failed_calls_per_run = sum(r.tools.failed_calls for r in runs) / s.runs
    s.repeated_reads_per_run = sum(r.tools.repeated_reads for r in runs) / s.runs

    s.input_tokens = sum(r.usage.input_tokens for r in runs)
    s.output_tokens = sum(r.usage.output_tokens for r in runs)
    s.cached_input_tokens = sum(r.usage.cached_input_tokens for r in runs)
    s.cache_write_tokens = sum(r.usage.cache_write_tokens for r in runs)
    s.input_token_cost = sum(r.cost.input_cost for r in runs)
    s.output_token_cost = sum(r.cost.output_cost for r in runs)
    savings = [r.cost.cache_savings for r in runs if r.cost.cache_savings is not None]
    s.cache_savings = sum(savings) if savings else None

    by_cat: dict[str, list[RunRecord]] = defaultdict(list)
    for r in runs:
        by_cat[r.task_category].append(r)
    for cat, cat_runs in sorted(by_cat.items()):
        cat_success = [r for r in cat_runs if r.result.success]
        cat_costs = _cost_values(cat_runs)
        s.by_category[cat] = CategorySummary(
            category=cat,
            runs=len(cat_runs),
            successes=len(cat_success),
            success_rate=len(cat_success) / len(cat_runs),
            success_rate_ci=wilson_interval(len(cat_success), len(cat_runs)),
            mean_score=statistics.fmean([r.score.composite for r in cat_runs]),
            mean_cost=statistics.fmean(cat_costs) if cat_costs else None,
            cost_per_successful_task=(
                sum(cat_costs) / len(cat_success) if cat_success and cat_costs else None
            ),
            mean_duration_s=statistics.fmean([r.time.wall_clock_ms / 1000.0 for r in cat_runs]),
            mean_tool_calls=statistics.fmean([float(r.tools.total_calls) for r in cat_runs]),
        )
    return s


class TaskCell(BaseModel):
    """One (task, target) cell: the unit that repetition statistics apply to."""

    task_id: str
    target_id: str
    runs: int = 0
    successes: int = 0
    success_rate: float = 0.0
    mean_score: float | None = None
    mean_cost: float | None = None
    mean_duration_s: float | None = None
    mean_tool_calls: float | None = None
    stdev_duration_s: float | None = None


class Comparison(BaseModel):
    targets: dict[str, TargetSummary] = Field(default_factory=dict)
    cells: list[TaskCell] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    total_runs: int = 0
    generated_at: str = ""
    recommendations: list[dict[str, Any]] = Field(default_factory=list)


def build_comparison(runs: list[RunRecord]) -> Comparison:
    from acb.models.run import utcnow

    comp = Comparison(total_runs=len(runs), generated_at=utcnow())
    by_target: dict[str, list[RunRecord]] = defaultdict(list)
    by_cell: dict[tuple[str, str], list[RunRecord]] = defaultdict(list)
    for r in runs:
        by_target[r.target_id].append(r)
        by_cell[(r.task_id, r.target_id)].append(r)

    for target_id, target_runs in by_target.items():
        comp.targets[target_id] = summarize_target(target_id, target_runs)

    for (task_id, target_id), cell_runs in sorted(by_cell.items()):
        costs = _cost_values(cell_runs)
        durations = [r.time.wall_clock_ms / 1000.0 for r in cell_runs]
        successes = sum(1 for r in cell_runs if r.result.success)
        comp.cells.append(
            TaskCell(
                task_id=task_id,
                target_id=target_id,
                runs=len(cell_runs),
                successes=successes,
                success_rate=successes / len(cell_runs),
                mean_score=statistics.fmean([r.score.composite for r in cell_runs]),
                mean_cost=statistics.fmean(costs) if costs else None,
                mean_duration_s=statistics.fmean(durations),
                mean_tool_calls=statistics.fmean([float(r.tools.total_calls) for r in cell_runs]),
                stdev_duration_s=statistics.stdev(durations) if len(durations) > 1 else 0.0,
            )
        )

    comp.categories = sorted({r.task_category for r in runs})
    comp.task_ids = sorted({r.task_id for r in runs})
    comp.recommendations = build_recommendations(comp)
    return comp


def build_recommendations(comp: Comparison) -> list[dict[str, Any]]:
    """Per-category routing advice: which target to use for which kind of work.

    This is the decision-oriented output the project exists for -- the answer is usually a
    routing strategy, not one universal model.
    """
    recs: list[dict[str, Any]] = []
    for category in comp.categories:
        candidates = []
        for target_id, summary in comp.targets.items():
            cat = summary.by_category.get(category)
            if cat and cat.runs:
                candidates.append((target_id, cat, summary))
        if not candidates:
            continue

        best_quality = max(candidates, key=lambda c: (c[1].success_rate, c[1].mean_score or 0.0))
        priced = [c for c in candidates if c[1].cost_per_successful_task is not None]
        best_value = (
            min(priced, key=lambda c: c[1].cost_per_successful_task) if priced else None
        )

        rec: dict[str, Any] = {
            "category": category,
            "best_quality_target": best_quality[0],
            "best_quality_success_rate": best_quality[1].success_rate,
            "best_quality_ci": best_quality[1].success_rate_ci,
        }
        if best_value:
            rec["best_value_target"] = best_value[0]
            rec["best_value_cost_per_success"] = best_value[1].cost_per_successful_task
            rec["cheaper_and_as_good"] = (
                best_value[0] != best_quality[0]
                # "As good" means the cheaper target's confidence interval overlaps the
                # best target's -- i.e. the observed gap is not statistically resolved.
                and best_value[1].success_rate_ci[1] >= best_quality[1].success_rate_ci[0]
            )
        recs.append(rec)
    return recs
