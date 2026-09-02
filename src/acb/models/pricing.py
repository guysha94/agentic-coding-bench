"""Pricing configuration.

Hard rule: **no price is ever hardcoded in source**. Every rate comes from configuration,
and every run stores a snapshot of the configuration it used so historical results stay
reproducible when provider prices change.

A `None` rate means *unknown*, not *free*. Costs computed with a missing rate are flagged
`incomplete` so a gap in configuration can never masquerade as a cheap target.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field, model_validator


class TokenPricing(BaseModel):
    """Per-million-token API pricing, split by token class."""

    type: Literal["token"] = "token"
    currency: str = "USD"
    effective_date: str | None = Field(
        default=None, description="ISO date the rates were taken from the provider."
    )
    source: str | None = Field(default=None, description="Where the rates came from.")

    input_per_million_tokens: float | None = None
    output_per_million_tokens: float | None = None
    cached_input_per_million_tokens: float | None = None
    cache_write_per_million_tokens: float | None = None
    reasoning_per_million_tokens: float | None = Field(
        default=None,
        description="Only set when the provider bills reasoning tokens SEPARATELY from "
        "output tokens. When set, reasoning tokens are subtracted from the output count "
        "before pricing, to avoid double-counting.",
    )
    extra_token_classes: dict[str, float] = Field(
        default_factory=dict,
        description="Provider-specific billable classes, e.g. {'web_search_per_call': 0.01}. "
        "Matched against NormalizedUsage.extra by key.",
    )

    def missing_rates(self) -> list[str]:
        """Rates that are required for a complete cost, but unset."""
        missing = []
        if self.input_per_million_tokens is None:
            missing.append("input_per_million_tokens")
        if self.output_per_million_tokens is None:
            missing.append("output_per_million_tokens")
        return missing


class GpuSpec(BaseModel):
    type: str
    count: int = 1
    hourly_cost_per_gpu: float

    @model_validator(mode="after")
    def _check(self) -> GpuSpec:
        if self.count < 1:
            raise ValueError("gpu.count must be >= 1")
        if self.hourly_cost_per_gpu < 0:
            raise ValueError("gpu.hourly_cost_per_gpu must be >= 0")
        return self


class InfrastructurePricing(BaseModel):
    """Self-hosted deployment priced by wall-clock time on owned/rented hardware.

    Produces two figures that must never be conflated:

    * marginal run cost -- infrastructure time this run directly consumed.
    * effective cost    -- marginal / utilization, i.e. what the capacity really costs
                           given that idle GPUs still bill.
    """

    type: Literal["infrastructure"] = "infrastructure"
    currency: str = "USD"
    effective_date: str | None = None
    source: str | None = None

    gpu: GpuSpec | None = None
    total_hourly_cost: float | None = Field(
        default=None, description="Flat hourly rate; alternative to the gpu/machine breakdown."
    )
    additional_machine_hourly_cost: float = 0.0
    utilization_assumption: float | None = Field(
        default=None,
        description="Fraction in (0, 1]. Used only for effective cost; never for marginal.",
    )

    # --- extensibility hooks (documented, applied when set) ---------------------------
    minimum_billing_seconds: int | None = Field(
        default=None, description="Round runtime up to this granularity before pricing."
    )
    discount_multiplier: float = Field(
        default=1.0, description="Spot/reserved/committed-use discount, e.g. 0.4 for 60% off."
    )
    storage_hourly_cost: float = 0.0
    network_cost_per_run: float = 0.0

    @model_validator(mode="after")
    def _check(self) -> InfrastructurePricing:
        if self.gpu is None and self.total_hourly_cost is None:
            raise ValueError("infrastructure pricing requires either `gpu` or `total_hourly_cost`")
        u = self.utilization_assumption
        if u is not None and not (0 < u <= 1):
            raise ValueError("utilization_assumption must be in (0, 1]")
        if self.discount_multiplier < 0:
            raise ValueError("discount_multiplier must be >= 0")
        return self

    def raw_hourly_cost(self) -> float:
        """Nominal hourly cost of the deployment, before utilisation adjustment."""
        if self.total_hourly_cost is not None:
            base = self.total_hourly_cost
        else:
            assert self.gpu is not None
            base = self.gpu.count * self.gpu.hourly_cost_per_gpu
        base += self.additional_machine_hourly_cost + self.storage_hourly_cost
        return base * self.discount_multiplier


AllocationStrategy = Literal["active_user", "observed_usage_share", "working_hours", "manual"]


class SubscriptionPricing(BaseModel):
    """Fixed monthly cost. Per-task cost is an ESTIMATE under a named assumption.

    There is no universally correct way to allocate a subscription to a task, so the
    framework refuses to pretend there is: it reports the raw monthly cost alongside the
    allocated figure and always labels the allocation with its strategy.
    """

    type: Literal["subscription"] = "subscription"
    currency: str = "USD"
    effective_date: str | None = None
    source: str | None = None

    monthly_cost: float
    allocation_strategy: AllocationStrategy = "active_user"

    # Strategy inputs.
    seats: int = Field(default=1, description="active_user: seats sharing the subscription.")
    tasks_per_month: float | None = Field(
        default=None, description="active_user: expected successful tasks per seat per month."
    )
    working_hours_per_month: float = Field(
        default=160.0, description="working_hours: billable hours in a month."
    )
    manual_cost_per_run: float | None = Field(
        default=None, description="manual: operator-supplied per-run cost."
    )

    @model_validator(mode="after")
    def _check(self) -> SubscriptionPricing:
        if self.monthly_cost < 0:
            raise ValueError("monthly_cost must be >= 0")
        if self.allocation_strategy == "manual" and self.manual_cost_per_run is None:
            raise ValueError("allocation_strategy 'manual' requires manual_cost_per_run")
        if self.seats < 1:
            raise ValueError("seats must be >= 1")
        return self


class UnknownPricing(BaseModel):
    """Explicitly unpriced target: competes on quality, excluded from cost aggregates."""

    type: Literal["unknown"] = "unknown"
    currency: str = "USD"
    effective_date: str | None = None
    source: str | None = None
    note: str | None = None


PricingConfig = Annotated[
    TokenPricing | InfrastructurePricing | SubscriptionPricing | UnknownPricing,
    Field(discriminator="type"),
]
