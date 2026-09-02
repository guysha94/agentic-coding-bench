"""Provider-independent token usage, and the cost breakdown derived from it.

The critical concern here is **double counting**. Providers disagree about whether cached
tokens sit inside or beside the input count, and whether reasoning tokens sit inside or
beside the output count. Normalisation is an explicit, versioned, per-adapter
responsibility (see `acb.adapters`), and the raw provider payload is never discarded.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Bump when normalisation semantics change. Stored per run so historical runs can be
# re-normalised correctly rather than reinterpreted under new rules.
NORMALIZATION_VERSION = "1.0.0"


class NormalizedUsage(BaseModel):
    """Token counts with a single, unambiguous meaning across all providers.

    Invariants (enforced by adapters, asserted in tests):

    * `input_tokens` are BILLABLE, NON-CACHED prompt tokens. Cache reads are NOT included.
    * `cached_input_tokens` are cache READS (discounted).
    * `cache_write_tokens` are cache CREATION (often a premium).
    * `reasoning_tokens` are a SUBSET of `output_tokens`. They are only priced separately
      when the target configures a distinct reasoning rate, in which case they are
      subtracted from output first.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_turns: int = 0

    extra: dict[str, float] = Field(
        default_factory=dict,
        description="Provider-specific billable counters, e.g. web_search_requests.",
    )
    normalization_version: str = NORMALIZATION_VERSION
    dialect: str = "unknown"
    complete: bool = Field(
        default=True,
        description="False when the provider omitted fields we expected (e.g. no cache "
        "reporting behind a proxy). Propagates into cost as `incomplete`.",
    )
    notes: list[str] = Field(default_factory=list)

    @property
    def total_billable_input(self) -> int:
        return self.input_tokens + self.cached_input_tokens + self.cache_write_tokens

    @property
    def total_tokens(self) -> int:
        return self.total_billable_input + self.output_tokens

    def __add__(self, other: NormalizedUsage) -> NormalizedUsage:
        extra = dict(self.extra)
        for k, v in other.extra.items():
            extra[k] = extra.get(k, 0.0) + v
        return NormalizedUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            cache_write_tokens=self.cache_write_tokens + other.cache_write_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            total_turns=self.total_turns + other.total_turns,
            extra=extra,
            normalization_version=self.normalization_version,
            dialect=self.dialect if self.dialect == other.dialect else "mixed",
            complete=self.complete and other.complete,
            notes=sorted(set(self.notes) | set(other.notes)),
        )


class CostBreakdown(BaseModel):
    """Cost of one run, decomposed so cache savings and infra assumptions stay visible."""

    currency: str = "USD"
    pricing_type: str = "unknown"

    # Token pricing components.
    input_cost: float = 0.0
    output_cost: float = 0.0
    cached_input_cost: float = 0.0
    cache_write_cost: float = 0.0
    reasoning_cost: float = 0.0
    extra_cost: float = 0.0

    # Infrastructure pricing components. Marginal and effective are deliberately separate
    # figures and are never summed together.
    marginal_infrastructure_cost: float | None = None
    effective_infrastructure_cost: float | None = None
    utilization_assumption: float | None = None

    # Subscription pricing components.
    monthly_subscription_cost: float | None = None
    allocated_subscription_cost: float | None = None
    allocation_strategy: str | None = None

    total_cost: float | None = Field(
        default=None,
        description="Headline cost for this run under its pricing type. None when the "
        "pricing type is `unknown` -- the run is then excluded from cost aggregates.",
    )
    is_estimate: bool = False
    incomplete: bool = Field(
        default=False, description="True when required rates or usage fields were missing."
    )
    warnings: list[str] = Field(default_factory=list)

    cache_savings: float | None = Field(
        default=None,
        description="What cache reads saved versus paying full input price for the same "
        "tokens. None when either rate is unknown -- we never imply a saving we cannot "
        "substantiate.",
    )


class RawUsageRecord(BaseModel):
    """Untouched provider payload, preserved so normalisation can be fixed retroactively."""

    provider: str
    model: str
    payloads: list[dict[str, Any]] = Field(default_factory=list)
    result_event: dict[str, Any] | None = None
