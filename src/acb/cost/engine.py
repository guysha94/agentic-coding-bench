"""Cost engine.

Turns `NormalizedUsage` + runtime + a pricing configuration into a `CostBreakdown`.

Design rules, all load-bearing:

* **No prices in source.** Every rate arrives from configuration.
* **`None` means unknown, not free.** A missing required rate marks the cost `incomplete`
  instead of understating it.
* **No double counting.** Usage is normalised upstream; reasoning tokens are only removed
  from the output count when a separate reasoning rate exists.
* **Marginal and effective infrastructure cost are separate figures** and are never summed.
* **Subscription allocation is an estimate** and is always labelled as one.
"""

from __future__ import annotations

from acb.models.pricing import (
    InfrastructurePricing,
    SubscriptionPricing,
    TokenPricing,
    UnknownPricing,
)
from acb.models.usage import CostBreakdown, NormalizedUsage

_PER_MILLION = 1_000_000.0


def _rate_cost(tokens: int, rate: float | None) -> tuple[float, bool]:
    """Cost for `tokens` at `rate` per million. Returns (cost, rate_was_missing)."""
    if tokens <= 0:
        return 0.0, False
    if rate is None:
        return 0.0, True
    return tokens / _PER_MILLION * rate, False


def compute_token_cost(usage: NormalizedUsage, pricing: TokenPricing) -> CostBreakdown:
    b = CostBreakdown(currency=pricing.currency, pricing_type="token")
    missing: list[str] = []

    # Reasoning tokens are a subset of output tokens. Only carve them out when the provider
    # bills them separately; otherwise they are already paid for as output.
    output_tokens = usage.output_tokens
    reasoning_tokens = 0
    if pricing.reasoning_per_million_tokens is not None and usage.reasoning_tokens > 0:
        reasoning_tokens = min(usage.reasoning_tokens, output_tokens)
        output_tokens -= reasoning_tokens

    b.input_cost, m = _rate_cost(usage.input_tokens, pricing.input_per_million_tokens)
    if m:
        missing.append("input")
    b.output_cost, m = _rate_cost(output_tokens, pricing.output_per_million_tokens)
    if m:
        missing.append("output")
    b.cached_input_cost, m = _rate_cost(
        usage.cached_input_tokens, pricing.cached_input_per_million_tokens
    )
    if m:
        missing.append("cached_input")
    b.cache_write_cost, m = _rate_cost(
        usage.cache_write_tokens, pricing.cache_write_per_million_tokens
    )
    if m:
        missing.append("cache_write")
    b.reasoning_cost, m = _rate_cost(reasoning_tokens, pricing.reasoning_per_million_tokens)
    if m:
        missing.append("reasoning")

    for key, rate in pricing.extra_token_classes.items():
        count = usage.extra.get(key)
        if count:
            b.extra_cost += count * rate

    b.total_cost = (
        b.input_cost
        + b.output_cost
        + b.cached_input_cost
        + b.cache_write_cost
        + b.reasoning_cost
        + b.extra_cost
    )

    # Cache savings only mean something when both rates are known.
    if (
        usage.cached_input_tokens
        and pricing.input_per_million_tokens is not None
        and pricing.cached_input_per_million_tokens is not None
    ):
        full_price = usage.cached_input_tokens / _PER_MILLION * pricing.input_per_million_tokens
        b.cache_savings = full_price - b.cached_input_cost

    if missing:
        b.incomplete = True
        b.warnings.append(
            "missing pricing rate(s) for token class(es): "
            + ", ".join(sorted(set(missing)))
            + " -- reported cost is a LOWER BOUND"
        )
    if not usage.complete:
        b.incomplete = True
        b.warnings.extend(usage.notes)
    return b


def compute_infrastructure_cost(
    runtime_seconds: float, pricing: InfrastructurePricing
) -> CostBreakdown:
    b = CostBreakdown(currency=pricing.currency, pricing_type="infrastructure")

    billed_seconds = runtime_seconds
    if pricing.minimum_billing_seconds:
        unit = pricing.minimum_billing_seconds
        # Round up to the next billing unit, as cloud providers do.
        billed_seconds = -(-runtime_seconds // unit) * unit
        b.warnings.append(
            f"runtime rounded up to a {unit}s minimum billing interval "
            f"({runtime_seconds:.1f}s -> {billed_seconds:.0f}s)"
        )
    runtime_hours = billed_seconds / 3600.0

    hourly = pricing.raw_hourly_cost()
    marginal = runtime_hours * hourly + pricing.network_cost_per_run
    b.marginal_infrastructure_cost = marginal

    u = pricing.utilization_assumption
    if u:
        # An idle GPU still bills, so usable capacity costs more than the nominal rate.
        b.effective_infrastructure_cost = marginal / u
        b.utilization_assumption = u
        b.is_estimate = True
        b.warnings.append(
            f"effective cost assumes {u:.0%} GPU utilisation; marginal cost assumes the run "
            "consumed dedicated capacity"
        )
        b.total_cost = b.effective_infrastructure_cost
    else:
        b.total_cost = marginal
        b.warnings.append(
            "no utilization_assumption configured; headline cost is MARGINAL cost and "
            "understates the cost of dedicated capacity"
        )
    return b


def compute_subscription_cost(
    pricing: SubscriptionPricing,
    runtime_seconds: float,
    successful_tasks_in_batch: int | None = None,
    runs_in_batch: int | None = None,
) -> CostBreakdown:
    b = CostBreakdown(currency=pricing.currency, pricing_type="subscription")
    b.monthly_subscription_cost = pricing.monthly_cost
    b.allocation_strategy = pricing.allocation_strategy
    b.is_estimate = True

    strategy = pricing.allocation_strategy
    per_seat_monthly = pricing.monthly_cost / pricing.seats

    if strategy == "manual":
        allocated = pricing.manual_cost_per_run or 0.0

    elif strategy == "active_user":
        if pricing.tasks_per_month and pricing.tasks_per_month > 0:
            allocated = per_seat_monthly / pricing.tasks_per_month
        else:
            allocated = 0.0
            b.incomplete = True
            b.warnings.append(
                "allocation_strategy 'active_user' needs tasks_per_month to allocate cost"
            )

    elif strategy == "working_hours":
        hourly = (
            per_seat_monthly / pricing.working_hours_per_month
            if pricing.working_hours_per_month
            else 0.0
        )
        allocated = hourly * (runtime_seconds / 3600.0)

    elif strategy == "observed_usage_share":
        # Spread the seat's monthly cost across the runs actually observed in this batch.
        denom = successful_tasks_in_batch or runs_in_batch
        if denom:
            allocated = per_seat_monthly / denom
            b.warnings.append(
                f"cost allocated across {denom} observed run(s) in this batch; the figure "
                "changes as the batch grows"
            )
        else:
            allocated = 0.0
            b.incomplete = True
            b.warnings.append("no observed runs available to allocate subscription cost across")
    else:  # pragma: no cover - guarded by the pydantic Literal
        allocated = 0.0
        b.incomplete = True
        b.warnings.append(f"unknown allocation strategy {strategy!r}")

    b.allocated_subscription_cost = allocated
    b.total_cost = allocated
    b.warnings.append(
        f"ESTIMATE: subscription cost allocated per run using strategy {strategy!r}; "
        "raw monthly cost is reported separately and is the only measured figure"
    )
    return b


def compute_cost(
    usage: NormalizedUsage,
    pricing: TokenPricing | InfrastructurePricing | SubscriptionPricing | UnknownPricing,
    runtime_seconds: float,
    successful_tasks_in_batch: int | None = None,
    runs_in_batch: int | None = None,
) -> CostBreakdown:
    """Dispatch to the right pricing model for this target."""
    if isinstance(pricing, TokenPricing):
        return compute_token_cost(usage, pricing)
    if isinstance(pricing, InfrastructurePricing):
        return compute_infrastructure_cost(runtime_seconds, pricing)
    if isinstance(pricing, SubscriptionPricing):
        return compute_subscription_cost(
            pricing, runtime_seconds, successful_tasks_in_batch, runs_in_batch
        )
    return CostBreakdown(
        currency=pricing.currency,
        pricing_type="unknown",
        total_cost=None,
        incomplete=True,
        warnings=[
            pricing.note
            or "pricing type is 'unknown'; this target is excluded from cost aggregates"
        ],
    )
