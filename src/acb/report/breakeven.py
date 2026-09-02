"""Break-even analysis: API inference vs self-hosted deployment.

The question this answers: *above what workload does owning GPUs beat paying per token?*

Method -- deliberately simple and fully explicit, because every input is an assumption:

  1. From observed API runs, derive cost per successful task (measured).
  2. From observed self-hosted runs, derive throughput (successful tasks per hour of
     wall-clock) -- measured -- and combine it with the configured infrastructure hourly
     rate to get cost per successful task at a given utilisation.
  3. Self-hosting wins when
         daily_tasks x api_cost_per_task  >=  24h x hourly_infra_cost / utilisation
     i.e. when the API bill exceeds the cost of keeping the hardware running.

Everything here is an ESTIMATE derived from observed benchmark throughput and configured
infrastructure assumptions, and is labelled as such in every output.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from acb.models.pricing import InfrastructurePricing
from acb.models.run import RunRecord
from acb.models.target import TargetConfig


class UtilizationScenario(BaseModel):
    utilization: float
    hourly_capacity_cost: float
    cost_per_successful_task: float | None = None
    break_even_tasks_per_day: float | None = None
    break_even_tokens_per_day: float | None = None


class BreakEvenAnalysis(BaseModel):
    api_target: str
    self_hosted_target: str
    currency: str = "USD"

    api_runs: int = 0
    api_successes: int = 0
    api_cost_per_successful_task: float | None = None
    api_mean_input_tokens: float | None = None
    api_mean_output_tokens: float | None = None
    api_mean_cached_tokens: float | None = None

    self_hosted_runs: int = 0
    self_hosted_successes: int = 0
    observed_successful_tasks_per_hour: float | None = None
    observed_tokens_per_successful_task: float | None = None
    raw_hourly_cost: float | None = None

    scenarios: list[UtilizationScenario] = Field(default_factory=list)
    conclusion: str = ""
    caveats: list[str] = Field(default_factory=list)
    sufficient_data: bool = False


def analyze_break_even(
    api_target: TargetConfig,
    api_runs: list[RunRecord],
    self_hosted_target: TargetConfig,
    self_hosted_runs: list[RunRecord],
    utilizations: list[float] | None = None,
) -> BreakEvenAnalysis:
    analysis = BreakEvenAnalysis(
        api_target=api_target.id,
        self_hosted_target=self_hosted_target.id,
        currency=api_target.pricing.currency,
    )
    utilizations = utilizations or [0.1, 0.3, 0.5, 0.7, 0.9]

    # --- API side (measured) ---------------------------------------------------------
    analysis.api_runs = len(api_runs)
    api_success = [r for r in api_runs if r.result.success]
    analysis.api_successes = len(api_success)
    api_costs = [r.cost.total_cost for r in api_runs if r.cost.total_cost is not None]
    if api_success and api_costs:
        analysis.api_cost_per_successful_task = sum(api_costs) / len(api_success)
    if api_runs:
        n = len(api_runs)
        analysis.api_mean_input_tokens = sum(r.usage.input_tokens for r in api_runs) / n
        analysis.api_mean_output_tokens = sum(r.usage.output_tokens for r in api_runs) / n
        analysis.api_mean_cached_tokens = sum(r.usage.cached_input_tokens for r in api_runs) / n

    # --- Self-hosted side (measured throughput + configured cost) --------------------
    analysis.self_hosted_runs = len(self_hosted_runs)
    sh_success = [r for r in self_hosted_runs if r.result.success]
    analysis.self_hosted_successes = len(sh_success)

    total_hours = sum(r.time.wall_clock_ms for r in self_hosted_runs) / 1000.0 / 3600.0
    if sh_success and total_hours > 0:
        analysis.observed_successful_tasks_per_hour = len(sh_success) / total_hours
        analysis.observed_tokens_per_successful_task = (
            sum(r.usage.total_tokens for r in self_hosted_runs) / len(sh_success)
        )

    pricing = self_hosted_target.pricing
    if not isinstance(pricing, InfrastructurePricing):
        analysis.conclusion = (
            f"target {self_hosted_target.id!r} does not use infrastructure pricing "
            f"(found {pricing.type!r}); break-even analysis needs an hourly hardware cost."
        )
        return analysis

    hourly = pricing.raw_hourly_cost()
    analysis.raw_hourly_cost = hourly

    have_api = analysis.api_cost_per_successful_task is not None
    have_throughput = analysis.observed_successful_tasks_per_hour is not None

    for u in utilizations:
        scenario = UtilizationScenario(utilization=u, hourly_capacity_cost=hourly / u)
        if have_throughput:
            # Cost of one successful task = hours it occupies x cost of that capacity.
            scenario.cost_per_successful_task = (
                hourly / u / analysis.observed_successful_tasks_per_hour
            )
        if have_api and analysis.api_cost_per_successful_task:
            # Daily API spend must exceed the daily cost of keeping the hardware up.
            daily_capacity_cost = 24.0 * hourly / u
            scenario.break_even_tasks_per_day = (
                daily_capacity_cost / analysis.api_cost_per_successful_task
            )
            if analysis.observed_tokens_per_successful_task:
                scenario.break_even_tokens_per_day = (
                    scenario.break_even_tasks_per_day
                    * analysis.observed_tokens_per_successful_task
                )
        analysis.scenarios.append(scenario)

    analysis.sufficient_data = bool(have_api and have_throughput)
    if analysis.sufficient_data:
        viable = [
            s
            for s in analysis.scenarios
            if s.cost_per_successful_task is not None
            and s.cost_per_successful_task < (analysis.api_cost_per_successful_task or 0)
        ]
        if viable:
            best = min(viable, key=lambda s: s.utilization)
            analysis.conclusion = (
                f"ESTIMATE: self-hosting {self_hosted_target.id} becomes cheaper than "
                f"{api_target.id} at or above {best.utilization:.0%} utilisation, which "
                f"corresponds to roughly {best.break_even_tasks_per_day:,.0f} successful "
                f"coding tasks/day"
                + (
                    f" (~{best.break_even_tokens_per_day / 1e6:,.1f}M tokens/day)"
                    if best.break_even_tokens_per_day
                    else ""
                )
                + "."
            )
        else:
            analysis.conclusion = (
                f"ESTIMATE: at every utilisation tested ({utilizations[0]:.0%}-"
                f"{utilizations[-1]:.0%}), self-hosting {self_hosted_target.id} costs more "
                f"per successful task than {api_target.id}. API inference is the cheaper "
                "option at the observed throughput."
            )
    else:
        missing = []
        if not have_api:
            missing.append("API cost per successful task (no priced successful API runs)")
        if not have_throughput:
            missing.append("self-hosted throughput (no successful self-hosted runs)")
        analysis.conclusion = "Insufficient data: missing " + "; ".join(missing) + "."

    analysis.caveats = [
        "All figures are ESTIMATES from observed benchmark throughput and configured "
        "infrastructure assumptions, not from production traffic.",
        "Benchmark wall-clock includes agent thinking, tool execution and test runs, so "
        "observed throughput understates the GPU's raw token throughput.",
        "Concurrency is not modelled: a real server batches multiple sessions, which "
        "raises effective throughput and moves the break-even point down.",
        "Model quality is NOT part of this calculation. Check the success rates before "
        "acting on cost alone -- a cheaper target that fails more can cost more in practice.",
    ]
    return analysis
