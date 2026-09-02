"""Aggregation, statistics, break-even analysis and report rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from acb.models.pricing import GpuSpec, InfrastructurePricing, TokenPricing
from acb.models.run import OutcomeMetrics, RunRecord, ScoreBreakdown, TimeMetrics, ToolMetrics
from acb.models.target import TargetConfig
from acb.models.usage import CostBreakdown, NormalizedUsage
from acb.report.aggregate import (
    Stats,
    build_comparison,
    percentile,
    summarize_target,
    wilson_interval,
)
from acb.report.breakeven import analyze_break_even
from acb.report.html import render_csv, render_html, write_report


def run(
    run_id: str, target: str = "t1", success: bool = True, cost: float | None = 0.10,
    duration_s: float = 60.0, tools: int = 20, category: str = "bug_fix",
    task: str = "task-1", score: float = 0.8, failed_calls: int = 0,
    repeated_reads: int = 0, mcp_calls: int = 0, outcome: str | None = None,
    input_tokens: int = 1000, output_tokens: int = 200, cached: int = 5000,
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        task_id=task,
        task_category=category,
        target_id=target,
        model=f"model-{target}",
        model_family=f"family-{target}",
        provider=f"provider-{target}",
        outcome=outcome or ("success" if success else "failure"),
        usage=NormalizedUsage(
            input_tokens=input_tokens, output_tokens=output_tokens, cached_input_tokens=cached
        ),
        cost=CostBreakdown(pricing_type="token", total_cost=cost, cache_savings=0.01),
        tools=ToolMetrics(
            total_calls=tools, failed_calls=failed_calls,
            repeated_reads=repeated_reads, mcp_calls=mcp_calls,
        ),
        time=TimeMetrics(wall_clock_ms=int(duration_s * 1000)),
        result=OutcomeMetrics(success=success),
        score=ScoreBreakdown(composite=score),
    )


class TestStatistics:
    def test_wilson_interval_brackets_the_estimate(self):
        low, high = wilson_interval(8, 10)
        assert low < 0.8 < high

    def test_wilson_interval_is_wider_at_small_n(self):
        narrow = wilson_interval(80, 100)
        wide = wilson_interval(8, 10)
        assert (wide[1] - wide[0]) > (narrow[1] - narrow[0])

    def test_wilson_interval_handles_extremes(self):
        assert wilson_interval(0, 5)[0] == 0.0
        assert wilson_interval(5, 5)[1] == 1.0
        assert wilson_interval(0, 0) == (0.0, 1.0)

    def test_percentiles(self):
        values = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert percentile(values, 0.5) == 3.0
        assert percentile(values, 0.0) == 1.0
        assert percentile(values, 1.0) == 5.0
        assert percentile([], 0.5) is None
        assert percentile([7.0], 0.9) == 7.0

    def test_stats_summary(self):
        stats = Stats.of([1.0, 2.0, 3.0])
        assert stats.n == 3
        assert stats.mean == pytest.approx(2.0)
        assert stats.median == 2.0
        assert stats.total == 6.0
        assert stats.stdev == pytest.approx(1.0)

    def test_stats_of_single_value_has_zero_stdev(self):
        assert Stats.of([5.0]).stdev == 0.0

    def test_stats_of_empty_is_empty(self):
        assert Stats.of([]).n == 0
        assert Stats.of([]).mean is None


class TestTargetSummary:
    def test_success_rate_and_counts(self):
        runs = [run("a", success=True), run("b", success=True), run("c", success=False)]
        s = summarize_target("t1", runs)
        assert s.runs == 3
        assert s.successes == 2
        assert s.success_rate == pytest.approx(2 / 3)
        assert s.success_rate_ci[0] < s.success_rate < s.success_rate_ci[1]

    def test_cost_per_successful_task_is_total_over_successes(self):
        """The decision-grade metric: failures still cost money."""
        runs = [run("a", success=True, cost=1.0), run("b", success=False, cost=1.0)]
        s = summarize_target("t1", runs)
        assert s.total_cost == pytest.approx(2.0)
        assert s.cost_per_successful_task == pytest.approx(2.0)

    def test_a_cheap_but_unreliable_target_can_cost_more_per_success(self):
        expensive = summarize_target("opus", [run(f"e{i}", success=True, cost=1.0)
                                              for i in range(4)])
        # 10x cheaper per run, but succeeds only 1 time in 20.
        cheap = summarize_target(
            "cheap",
            [run("c1", success=True, cost=0.1)] + [run(f"c{i}", success=False, cost=0.1)
                                                   for i in range(2, 21)]
        )
        assert cheap.cost.mean < expensive.cost.mean          # cheaper per run
        assert cheap.cost_per_successful_task == pytest.approx(2.0)
        assert expensive.cost_per_successful_task == pytest.approx(1.0)
        assert cheap.cost_per_successful_task > expensive.cost_per_successful_task

    def test_cost_per_failed_task(self):
        runs = [run("a", success=True, cost=1.0), run("b", success=False, cost=2.0)]
        assert summarize_target("t1", runs).cost_per_failed_task == pytest.approx(2.0)

    def test_unpriced_runs_yield_no_cost_figures(self):
        s = summarize_target("t1", [run("a", cost=None)])
        assert s.total_cost is None
        assert s.cost_per_successful_task is None

    def test_efficiency_ratios(self):
        runs = [run("a", success=True, tools=10, duration_s=60, mcp_calls=2),
                run("b", success=False, tools=30, duration_s=120, mcp_calls=4)]
        s = summarize_target("t1", runs)
        assert s.tool_calls_per_successful_task == pytest.approx(40.0)
        assert s.wall_time_per_successful_task_s == pytest.approx(180.0)
        assert s.mcp_calls_per_successful_task == pytest.approx(6.0)

    def test_failure_signals_are_averaged_per_run(self):
        runs = [run("a", failed_calls=2, repeated_reads=4), run("b", failed_calls=0)]
        s = summarize_target("t1", runs)
        assert s.failed_calls_per_run == pytest.approx(1.0)
        assert s.repeated_reads_per_run == pytest.approx(2.0)

    def test_timeouts_and_errors_are_counted_distinctly(self):
        runs = [run("a", success=False, outcome="timeout"),
                run("b", success=False, outcome="error"),
                run("c", success=False, outcome="failure")]
        s = summarize_target("t1", runs)
        assert (s.timeouts, s.errors, s.failures) == (1, 1, 1)

    def test_token_totals_are_summed(self):
        s = summarize_target("t1", [run("a"), run("b")])
        assert s.input_tokens == 2000
        assert s.cached_input_tokens == 10000

    def test_per_category_breakdown(self):
        runs = [
            run("a", category="bug_fix", success=True),
            run("b", category="bug_fix", success=False),
            run("c", category="mcp", success=True),
        ]
        s = summarize_target("t1", runs)
        assert s.by_category["bug_fix"].success_rate == pytest.approx(0.5)
        assert s.by_category["mcp"].success_rate == 1.0

    def test_empty_run_list_is_safe(self):
        assert summarize_target("t1", []).runs == 0


class TestComparison:
    @pytest.fixture
    def comp(self):
        runs = [
            run("a1", target="opus", success=True, cost=1.0, category="bug_fix"),
            run("a2", target="opus", success=True, cost=1.0, category="mcp"),
            run("g1", target="glm", success=True, cost=0.05, category="bug_fix"),
            run("g2", target="glm", success=False, cost=0.05, category="mcp"),
        ]
        return build_comparison(runs)

    def test_targets_and_categories_are_collected(self, comp):
        assert set(comp.targets) == {"opus", "glm"}
        assert comp.categories == ["bug_fix", "mcp"]
        assert comp.total_runs == 4

    def test_cells_are_per_task_and_target(self, comp):
        assert len(comp.cells) == 2  # one task x two targets
        for cell in comp.cells:
            assert cell.runs == 2

    def test_cell_statistics_include_variance(self):
        runs = [run("a", duration_s=10), run("b", duration_s=50)]
        comp = build_comparison(runs)
        cell = comp.cells[0]
        assert cell.mean_duration_s == pytest.approx(30.0)
        assert cell.stdev_duration_s > 0

    def test_recommendations_are_generated_per_category(self, comp):
        categories = {r["category"] for r in comp.recommendations}
        assert categories == {"bug_fix", "mcp"}

    def test_routing_advice_flags_a_cheaper_equal_quality_target(self, comp):
        bug_fix = next(r for r in comp.recommendations if r["category"] == "bug_fix")
        # Both solved bug_fix; glm is far cheaper, so it should be the value pick.
        assert bug_fix["best_value_target"] == "glm"
        assert bug_fix["cheaper_and_as_good"] is True

    def test_routing_advice_respects_a_resolved_quality_gap(self):
        runs = [run(f"o{i}", target="opus", success=True, cost=1.0) for i in range(20)]
        runs += [run(f"g{i}", target="glm", success=False, cost=0.01) for i in range(20)]
        comp = build_comparison(runs)
        rec = comp.recommendations[0]
        assert rec["best_quality_target"] == "opus"
        assert rec.get("cheaper_and_as_good") is False


class TestBreakEven:
    @pytest.fixture
    def api_target(self):
        return TargetConfig(
            id="glm-api", model="glm", provider="fireworks",
            pricing=TokenPricing(input_per_million_tokens=0.5, output_per_million_tokens=1.5),
        )

    @pytest.fixture
    def sh_target(self):
        return TargetConfig(
            id="glm-sh", model="glm", provider="internal", deployment_type="self_hosted",
            endpoint="http://gpu:8000/v1",
            pricing=InfrastructurePricing(
                gpu=GpuSpec(type="B200", count=4, hourly_cost_per_gpu=4.50),
                additional_machine_hourly_cost=1.20,
            ),
        )

    def test_produces_a_scenario_per_utilization(self, api_target, sh_target):
        api_runs = [run(f"a{i}", target="glm-api", success=True, cost=0.50) for i in range(4)]
        sh_runs = [run(f"s{i}", target="glm-sh", success=True, cost=None, duration_s=360)
                   for i in range(4)]
        analysis = analyze_break_even(api_target, api_runs, sh_target, sh_runs, [0.2, 0.5])
        assert [s.utilization for s in analysis.scenarios] == [0.2, 0.5]
        assert analysis.sufficient_data is True

    def test_higher_utilization_lowers_cost_per_task(self, api_target, sh_target):
        api_runs = [run("a", target="glm-api", success=True, cost=0.50)]
        sh_runs = [run("s", target="glm-sh", success=True, cost=None, duration_s=360)]
        analysis = analyze_break_even(api_target, api_runs, sh_target, sh_runs, [0.2, 0.8])
        low, high = analysis.scenarios
        assert high.cost_per_successful_task < low.cost_per_successful_task

    def test_break_even_volume_is_computed(self, api_target, sh_target):
        api_runs = [run("a", target="glm-api", success=True, cost=0.50)]
        sh_runs = [run("s", target="glm-sh", success=True, cost=None, duration_s=360)]
        analysis = analyze_break_even(api_target, api_runs, sh_target, sh_runs, [0.5])
        scenario = analysis.scenarios[0]
        # 19.20/h at 50% utilisation -> 38.40/h -> 921.60/day; / $0.50 per task.
        assert scenario.break_even_tasks_per_day == pytest.approx(24 * 19.20 / 0.5 / 0.50)
        assert scenario.break_even_tokens_per_day is not None

    def test_observed_throughput_comes_from_real_runs(self, api_target, sh_target):
        sh_runs = [run(f"s{i}", target="glm-sh", success=True, cost=None, duration_s=1800)
                   for i in range(2)]
        analysis = analyze_break_even(api_target, [], sh_target, sh_runs)
        assert analysis.observed_successful_tasks_per_hour == pytest.approx(2.0)

    def test_insufficient_data_is_stated_not_guessed(self, api_target, sh_target):
        analysis = analyze_break_even(api_target, [], sh_target, [])
        assert analysis.sufficient_data is False
        assert "Insufficient data" in analysis.conclusion

    def test_non_infrastructure_target_is_rejected_clearly(self, api_target):
        other = TargetConfig(
            id="x", model="m", provider="p",
            pricing=TokenPricing(input_per_million_tokens=1, output_per_million_tokens=1),
        )
        analysis = analyze_break_even(api_target, [], other, [])
        assert "does not use infrastructure pricing" in analysis.conclusion

    def test_conclusion_and_caveats_are_labelled_as_estimates(self, api_target, sh_target):
        api_runs = [run("a", target="glm-api", success=True, cost=50.0)]
        sh_runs = [run("s", target="glm-sh", success=True, cost=None, duration_s=360)]
        analysis = analyze_break_even(api_target, api_runs, sh_target, sh_runs, [0.5])
        assert "ESTIMATE" in analysis.conclusion
        assert analysis.caveats
        assert any("quality" in c.lower() for c in analysis.caveats)


class TestRendering:
    @pytest.fixture
    def comp(self):
        return build_comparison([
            run("a1", target="opus", success=True, cost=1.0),
            run("a2", target="opus", success=True, cost=1.0),
            run("a3", target="opus", success=True, cost=1.0),
            run("g1", target="glm", success=True, cost=0.05, category="mcp", mcp_calls=3),
            run("g2", target="glm", success=False, cost=0.05, category="mcp", mcp_calls=9),
            run("g3", target="glm", success=False, cost=0.05),
        ])

    def test_html_is_self_contained_and_mentions_every_target(self, comp):
        html = render_html(comp)
        assert html.startswith("<!doctype html>")
        assert "opus" in html and "glm" in html
        assert "http://" not in html.replace("http://www.w3.org", "")  # no external assets

    def test_html_reports_cost_per_successful_task(self, comp):
        assert "Cost/success" in render_html(comp)

    def test_html_warns_about_small_samples(self):
        comp = build_comparison([run("a", target="solo")])
        assert "too few to distinguish" in render_html(comp)

    def test_html_includes_a_cost_vs_quality_plot(self, comp):
        html = render_html(comp)
        assert "<svg" in html and "cost per successful task" in html

    def test_html_includes_break_even_when_supplied(self, comp):
        from acb.report.breakeven import BreakEvenAnalysis, UtilizationScenario

        analysis = BreakEvenAnalysis(
            api_target="glm", self_hosted_target="glm-sh",
            scenarios=[UtilizationScenario(utilization=0.4, hourly_capacity_cost=48.0)],
            conclusion="ESTIMATE: something",
        )
        html = render_html(comp, analysis)
        assert "Break-even" in html and "ESTIMATE: something" in html

    def test_html_includes_mcp_section_when_mcp_runs_exist(self, comp):
        runs = [run("g1", target="glm", category="mcp", mcp_calls=3)]
        assert "MCP / external tool usage" in render_html(comp, runs=runs)

    def test_html_is_theme_aware(self, comp):
        html = render_html(comp)
        assert "prefers-color-scheme: dark" in html

    def test_csv_has_a_row_per_target(self, comp):
        lines = render_csv(comp).strip().splitlines()
        assert len(lines) == 3  # header + 2 targets
        assert "cost_per_successful_task" in lines[0]

    def test_write_report_emits_all_three_formats(self, comp, tmp_path: Path):
        paths = write_report(tmp_path / "out", comp)
        for key in ("html", "json", "csv"):
            assert paths[key].exists() and paths[key].stat().st_size > 0

    def test_json_report_is_machine_readable(self, comp, tmp_path: Path):
        import json

        paths = write_report(tmp_path / "out", comp)
        payload = json.loads(paths["json"].read_text())
        assert set(payload["targets"]) == {"opus", "glm"}
        assert payload["targets"]["opus"]["success_rate"] == 1.0
