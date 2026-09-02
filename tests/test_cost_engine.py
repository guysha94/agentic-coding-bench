"""Cost calculations across all four pricing models."""

from __future__ import annotations

import pytest

from acb.cost.engine import (
    compute_cost,
    compute_infrastructure_cost,
    compute_subscription_cost,
    compute_token_cost,
)
from acb.models.pricing import (
    GpuSpec,
    InfrastructurePricing,
    SubscriptionPricing,
    TokenPricing,
    UnknownPricing,
)
from acb.models.usage import NormalizedUsage


class TestTokenPricing:
    def test_arithmetic_matches_the_specified_formula(self, token_pricing):
        usage = NormalizedUsage(
            input_tokens=1_000_000,
            output_tokens=500_000,
            cached_input_tokens=2_000_000,
            cache_write_tokens=400_000,
        )
        cost = compute_token_cost(usage, token_pricing)
        assert cost.input_cost == pytest.approx(3.0)          # 1M / 1M * 3.00
        assert cost.output_cost == pytest.approx(7.5)         # 0.5M / 1M * 15.00
        assert cost.cached_input_cost == pytest.approx(0.6)   # 2M / 1M * 0.30
        assert cost.cache_write_cost == pytest.approx(1.5)    # 0.4M / 1M * 3.75
        assert cost.total_cost == pytest.approx(3.0 + 7.5 + 0.6 + 1.5)

    def test_zero_usage_costs_nothing(self, token_pricing):
        assert compute_token_cost(NormalizedUsage(), token_pricing).total_cost == 0.0

    def test_missing_rate_is_unknown_not_free(self):
        pricing = TokenPricing(input_per_million_tokens=3.0, output_per_million_tokens=None)
        cost = compute_token_cost(
            NormalizedUsage(input_tokens=1_000_000, output_tokens=1_000_000), pricing
        )
        assert cost.incomplete is True
        assert "LOWER BOUND" in cost.warnings[0]
        assert cost.output_cost == 0.0

    def test_unused_token_class_with_null_rate_is_not_flagged(self, token_pricing):
        """A null rate only matters when tokens of that class were actually used."""
        token_pricing.cache_write_per_million_tokens = None
        cost = compute_token_cost(
            NormalizedUsage(input_tokens=1000, output_tokens=100, cache_write_tokens=0),
            token_pricing,
        )
        assert cost.incomplete is False

    def test_reasoning_not_double_charged_when_no_separate_rate(self, token_pricing):
        """With no reasoning rate, reasoning tokens stay inside the output charge."""
        usage = NormalizedUsage(output_tokens=1_000_000, reasoning_tokens=800_000)
        cost = compute_token_cost(usage, token_pricing)
        assert cost.output_cost == pytest.approx(15.0)
        assert cost.reasoning_cost == 0.0
        assert cost.total_cost == pytest.approx(15.0)

    def test_reasoning_carved_out_when_priced_separately(self, token_pricing):
        token_pricing.reasoning_per_million_tokens = 30.0
        usage = NormalizedUsage(output_tokens=1_000_000, reasoning_tokens=800_000)
        cost = compute_token_cost(usage, token_pricing)
        # 200k output at 15, 800k reasoning at 30 -- each token billed exactly once.
        assert cost.output_cost == pytest.approx(3.0)
        assert cost.reasoning_cost == pytest.approx(24.0)
        assert cost.total_cost == pytest.approx(27.0)

    def test_reasoning_cannot_exceed_output(self, token_pricing):
        """Defensive: a provider reporting reasoning > output must not create a credit."""
        token_pricing.reasoning_per_million_tokens = 30.0
        usage = NormalizedUsage(output_tokens=100_000, reasoning_tokens=900_000)
        cost = compute_token_cost(usage, token_pricing)
        assert cost.output_cost == 0.0
        assert cost.reasoning_cost == pytest.approx(3.0)

    def test_cache_savings_computed_when_both_rates_known(self, token_pricing):
        usage = NormalizedUsage(cached_input_tokens=1_000_000)
        cost = compute_token_cost(usage, token_pricing)
        assert cost.cache_savings == pytest.approx(3.0 - 0.30)

    def test_cache_savings_is_none_when_a_rate_is_unknown(self, token_pricing):
        token_pricing.cached_input_per_million_tokens = None
        cost = compute_token_cost(NormalizedUsage(cached_input_tokens=1_000_000), token_pricing)
        assert cost.cache_savings is None

    def test_incomplete_usage_propagates_to_cost(self, token_pricing):
        usage = NormalizedUsage(
            input_tokens=1000, output_tokens=10, complete=False, notes=["no cache reporting"]
        )
        cost = compute_token_cost(usage, token_pricing)
        assert cost.incomplete is True
        assert "no cache reporting" in cost.warnings

    def test_extra_token_classes_are_priced(self):
        pricing = TokenPricing(
            input_per_million_tokens=0.0,
            output_per_million_tokens=0.0,
            extra_token_classes={"web_search_requests": 0.01},
        )
        usage = NormalizedUsage(extra={"web_search_requests": 25})
        assert compute_token_cost(usage, pricing).extra_cost == pytest.approx(0.25)


class TestInfrastructurePricing:
    def test_marginal_and_effective_are_separate_figures(self, infra_pricing):
        # 4 x 4.50 + 1.20 = 19.20/hour; one hour of runtime.
        cost = compute_infrastructure_cost(3600.0, infra_pricing)
        assert cost.marginal_infrastructure_cost == pytest.approx(19.20)
        assert cost.effective_infrastructure_cost == pytest.approx(19.20 / 0.40)
        # The headline is the effective figure, and the two are never summed.
        assert cost.total_cost == pytest.approx(48.0)
        assert cost.is_estimate is True

    def test_scales_linearly_with_runtime(self, infra_pricing):
        half = compute_infrastructure_cost(1800.0, infra_pricing)
        assert half.marginal_infrastructure_cost == pytest.approx(9.60)

    def test_without_utilization_headline_is_marginal_and_warns(self):
        pricing = InfrastructurePricing(total_hourly_cost=10.0)
        cost = compute_infrastructure_cost(3600.0, pricing)
        assert cost.total_cost == pytest.approx(10.0)
        assert cost.effective_infrastructure_cost is None
        assert any("MARGINAL" in w for w in cost.warnings)

    def test_flat_hourly_rate_alternative(self):
        pricing = InfrastructurePricing(total_hourly_cost=5.0, utilization_assumption=0.4)
        cost = compute_infrastructure_cost(3600.0, pricing)
        assert cost.marginal_infrastructure_cost == pytest.approx(5.0)
        assert cost.total_cost == pytest.approx(12.5)

    def test_minimum_billing_interval_rounds_up(self):
        pricing = InfrastructurePricing(total_hourly_cost=3600.0, minimum_billing_seconds=60)
        # 10s of work billed as 60s; at $1/second that is $60.
        cost = compute_infrastructure_cost(10.0, pricing)
        assert cost.marginal_infrastructure_cost == pytest.approx(60.0)
        assert any("minimum billing" in w for w in cost.warnings)

    def test_discount_multiplier_applies(self):
        pricing = InfrastructurePricing(total_hourly_cost=10.0, discount_multiplier=0.4)
        cost = compute_infrastructure_cost(3600.0, pricing)
        assert cost.marginal_infrastructure_cost == pytest.approx(4.0)

    def test_storage_and_network_are_included(self):
        pricing = InfrastructurePricing(
            total_hourly_cost=10.0, storage_hourly_cost=1.0, network_cost_per_run=0.5
        )
        cost = compute_infrastructure_cost(3600.0, pricing)
        assert cost.marginal_infrastructure_cost == pytest.approx(11.5)

    def test_requires_gpu_or_hourly_cost(self):
        with pytest.raises(ValueError, match="requires either"):
            InfrastructurePricing()

    def test_utilization_must_be_a_fraction(self):
        with pytest.raises(ValueError, match="utilization_assumption"):
            InfrastructurePricing(total_hourly_cost=1.0, utilization_assumption=1.5)

    def test_raw_hourly_cost_from_gpu_breakdown(self):
        pricing = InfrastructurePricing(
            gpu=GpuSpec(type="H100", count=8, hourly_cost_per_gpu=2.0),
            additional_machine_hourly_cost=3.0,
        )
        assert pricing.raw_hourly_cost() == pytest.approx(19.0)


class TestSubscriptionPricing:
    def test_active_user_allocation(self, subscription_pricing):
        cost = compute_subscription_cost(subscription_pricing, 600.0)
        assert cost.allocated_subscription_cost == pytest.approx(2.0)  # 200 / 100 tasks
        assert cost.monthly_subscription_cost == 200.0
        assert cost.is_estimate is True
        assert any("ESTIMATE" in w for w in cost.warnings)

    def test_seats_divide_the_monthly_cost(self, subscription_pricing):
        subscription_pricing.seats = 4
        cost = compute_subscription_cost(subscription_pricing, 600.0)
        assert cost.allocated_subscription_cost == pytest.approx(0.5)

    def test_active_user_without_task_count_is_incomplete(self):
        pricing = SubscriptionPricing(monthly_cost=200.0, tasks_per_month=None)
        cost = compute_subscription_cost(pricing, 600.0)
        assert cost.incomplete is True

    def test_working_hours_allocation_scales_with_runtime(self):
        pricing = SubscriptionPricing(
            monthly_cost=160.0, allocation_strategy="working_hours", working_hours_per_month=160.0
        )
        # $1/hour of work; a 30-minute run costs $0.50.
        cost = compute_subscription_cost(pricing, 1800.0)
        assert cost.allocated_subscription_cost == pytest.approx(0.5)

    def test_manual_allocation_uses_the_configured_value(self):
        pricing = SubscriptionPricing(
            monthly_cost=200.0, allocation_strategy="manual", manual_cost_per_run=0.75
        )
        assert compute_subscription_cost(pricing, 60.0).allocated_subscription_cost == 0.75

    def test_manual_requires_a_value(self):
        with pytest.raises(ValueError, match="manual_cost_per_run"):
            SubscriptionPricing(monthly_cost=200.0, allocation_strategy="manual")

    def test_observed_usage_share_divides_across_the_batch(self):
        pricing = SubscriptionPricing(
            monthly_cost=200.0, allocation_strategy="observed_usage_share"
        )
        cost = compute_subscription_cost(pricing, 60.0, successful_tasks_in_batch=40)
        assert cost.allocated_subscription_cost == pytest.approx(5.0)

    def test_observed_usage_share_without_observations_is_incomplete(self):
        pricing = SubscriptionPricing(
            monthly_cost=200.0, allocation_strategy="observed_usage_share"
        )
        assert compute_subscription_cost(pricing, 60.0).incomplete is True


class TestUnknownPricing:
    def test_total_cost_is_none_and_flagged(self, anthropic_target):
        cost = compute_cost(NormalizedUsage(input_tokens=1000), UnknownPricing(), 60.0)
        assert cost.total_cost is None
        assert cost.incomplete is True
        assert cost.pricing_type == "unknown"


class TestDispatch:
    def test_each_pricing_type_routes_correctly(
        self, token_pricing, infra_pricing, subscription_pricing
    ):
        usage = NormalizedUsage(input_tokens=1000, output_tokens=100)
        assert compute_cost(usage, token_pricing, 60.0).pricing_type == "token"
        assert compute_cost(usage, infra_pricing, 60.0).pricing_type == "infrastructure"
        assert compute_cost(usage, subscription_pricing, 60.0).pricing_type == "subscription"
        assert compute_cost(usage, UnknownPricing(), 60.0).pricing_type == "unknown"

    def test_currency_is_carried_through(self):
        pricing = TokenPricing(
            currency="EUR", input_per_million_tokens=1.0, output_per_million_tokens=1.0
        )
        assert compute_cost(NormalizedUsage(), pricing, 1.0).currency == "EUR"


class TestPricingSnapshot:
    def test_snapshot_captures_everything_needed_to_reprice_later(self, anthropic_target):
        snap = anthropic_target.pricing_snapshot()
        for key in (
            "target", "model", "provider", "pricing_type", "currency",
            "pricing_effective_date", "pricing_snapshot", "normalization_version",
        ):
            assert key in snap, f"snapshot is missing {key}"
        assert snap["pricing_snapshot"]["input_per_million_tokens"] == 3.0

    def test_snapshot_is_a_copy_not_a_live_reference(self, anthropic_target):
        snap = anthropic_target.pricing_snapshot()
        anthropic_target.pricing.input_per_million_tokens = 999.0
        assert snap["pricing_snapshot"]["input_per_million_tokens"] == 3.0
