"""Validation execution, regression detection, and composite scoring."""

from __future__ import annotations

from pathlib import Path

import pytest

from acb.eval.scoring import (
    CostContext,
    ScoringConfig,
    ScoringWeights,
    compute_score,
    evaluate_outcome,
    score_agent_efficiency,
    score_cost,
    score_functional_correctness,
    score_time,
)
from acb.eval.validation import detect_regressions, parse_test_counts, run_check, run_validation
from acb.models.run import OutcomeMetrics, TimeMetrics, ToolMetrics, ValidationResult
from acb.models.task import TaskSpec, ValidationCommand


class TestTestCountParsing:
    def test_pytest_summary(self):
        assert parse_test_counts("5 passed in 0.12s")[0] == 5

    def test_pytest_with_failures(self):
        passed, failed, total = parse_test_counts("2 failed, 18 passed in 0.03s")
        assert (passed, failed, total) == (18, 2, 20)

    def test_jest_summary(self):
        passed, failed, total = parse_test_counts("Tests: 1 failed, 5 passed, 6 total")
        assert (passed, failed, total) == (5, 1, 6)

    def test_go_test_summary(self):
        passed, failed, total = parse_test_counts("ok  \tpkg/a\t0.1s\nFAIL\tpkg/b\t0.2s")
        assert (passed, failed, total) == (1, 1, 2)

    def test_unparseable_output_is_not_fatal(self):
        assert parse_test_counts("something else entirely") == (None, None, None)


class TestCheckExecution:
    def test_passing_command(self, tmp_path: Path):
        check = ValidationCommand(name="ok", kind="custom", command="exit 0")
        result = run_check(check, tmp_path, tmp_path)
        assert result.passed is True
        assert result.exit_code == 0

    def test_failing_command(self, tmp_path: Path):
        check = ValidationCommand(name="bad", kind="custom", command="exit 7")
        result = run_check(check, tmp_path, tmp_path)
        assert result.passed is False
        assert result.exit_code == 7

    def test_expect_failure_inverts_the_result(self, tmp_path: Path):
        """Used for tasks where a check SHOULD still fail (e.g. reproducing a bug)."""
        check = ValidationCommand(
            name="must-fail", kind="custom", command="exit 1", expect_failure=True
        )
        assert run_check(check, tmp_path, tmp_path).passed is True

    def test_timeout_is_recorded_as_a_failure(self, tmp_path: Path):
        check = ValidationCommand(
            name="hang", kind="custom", command="sleep 30", timeout_seconds=1
        )
        result = run_check(check, tmp_path, tmp_path)
        assert result.timed_out is True
        assert result.passed is False

    def test_output_is_captured_as_a_tail(self, tmp_path: Path):
        check = ValidationCommand(name="echo", kind="custom", command="echo hello-from-check")
        assert "hello-from-check" in run_check(check, tmp_path, tmp_path).stdout_tail

    def test_secrets_are_redacted_from_captured_output(self, tmp_path: Path):
        check = ValidationCommand(name="leak", kind="custom", command="echo topsecretvalue")
        result = run_check(check, tmp_path, tmp_path, secrets=["topsecretvalue"])
        assert "topsecretvalue" not in result.stdout_tail
        assert "[REDACTED]" in result.stdout_tail

    def test_provided_files_are_copied_in(self, tmp_path: Path):
        task_dir = tmp_path / "taskdir"
        task_dir.mkdir()
        (task_dir / "hidden.py").write_text("marker = 1\n")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        check = ValidationCommand(
            name="hidden", kind="test", hidden=True, command="test -f tests/hidden.py",
            provides_files={"tests/hidden.py": "hidden.py"},
        )
        result = run_check(check, workspace, task_dir)
        assert result.passed is True
        assert (workspace / "tests" / "hidden.py").read_text() == "marker = 1\n"

    def test_missing_provided_file_is_an_error_not_a_crash(self, tmp_path: Path):
        check = ValidationCommand(
            name="hidden", kind="test", command="true", provides_files={"a.py": "nope.py"}
        )
        result = run_check(check, tmp_path, tmp_path)
        assert result.passed is False
        assert "not found" in result.error

    def test_provided_file_cannot_escape_the_workspace(self, tmp_path: Path):
        task_dir = tmp_path / "taskdir"
        task_dir.mkdir()
        (task_dir / "evil.py").write_text("x")
        workspace = tmp_path / "ws"
        workspace.mkdir()
        check = ValidationCommand(
            name="escape", kind="test", command="true",
            provides_files={"../../escaped.py": "evil.py"},
        )
        result = run_check(check, workspace, task_dir)
        assert result.passed is False
        assert "escapes the workspace" in result.error

    def test_baseline_phase_skips_hidden_checks(self, tmp_path: Path):
        """Hidden test files must not exist in the workspace while the agent works."""
        task = TaskSpec.model_validate({
            "id": "t", "name": "T", "category": "bug_fix",
            "repository": {"path": "./x"}, "task": {"prompt": "p"},
            "validation": [
                {"name": "visible", "kind": "test", "command": "true"},
                {"name": "secret", "kind": "test", "command": "true", "hidden": True},
            ],
        })
        baseline = run_validation(task, tmp_path, tmp_path, "baseline")
        assert [r.name for r in baseline] == ["visible"]
        post = run_validation(task, tmp_path, tmp_path, "post")
        assert [r.name for r in post] == ["visible", "secret"]


class TestRegressionDetection:
    def _r(self, name: str, passed: bool, phase: str) -> ValidationResult:
        return ValidationResult(
            name=name, kind="test", command="x", hidden=False, phase=phase, passed=passed
        )

    def test_pass_to_fail_is_a_regression(self):
        count, names = detect_regressions(
            [self._r("a", True, "baseline")], [self._r("a", False, "post")]
        )
        assert (count, names) == (1, ["a"])

    def test_already_failing_check_is_not_a_regression(self):
        """This is the whole point of the baseline: pre-existing failures are not the
        agent's fault."""
        count, names = detect_regressions(
            [self._r("a", False, "baseline")], [self._r("a", False, "post")]
        )
        assert (count, names) == (0, [])

    def test_fail_to_pass_is_not_a_regression(self):
        count, _ = detect_regressions(
            [self._r("a", False, "baseline")], [self._r("a", True, "post")]
        )
        assert count == 0

    def test_hidden_check_with_no_baseline_is_not_a_regression(self):
        count, _ = detect_regressions([], [self._r("hidden", False, "post")])
        assert count == 0

    def test_multiple_regressions_are_all_reported(self):
        baseline = [self._r("a", True, "baseline"), self._r("b", True, "baseline")]
        post = [self._r("a", False, "post"), self._r("b", False, "post")]
        count, names = detect_regressions(baseline, post)
        assert count == 2
        assert set(names) == {"a", "b"}


class TestOutcomeEvaluation:
    def _task(self, **success) -> TaskSpec:
        return TaskSpec.model_validate({
            "id": "t", "name": "T", "category": "bug_fix",
            "repository": {"path": "./x"}, "task": {"prompt": "p"},
            "validation": [{"name": "t", "kind": "test", "command": "true"}],
            "success": success or {},
        })

    def _post(self, name: str, kind: str, passed: bool, hidden: bool = False, **kw):
        return ValidationResult(
            name=name, kind=kind, command="x", hidden=hidden, phase="post", passed=passed, **kw
        )

    def test_all_gates_pass(self):
        outcome = evaluate_outcome(
            self._task(), [self._post("t", "test", True)], 0, [], False
        )
        assert outcome.success is True
        assert outcome.failure_reason is None

    def test_failing_tests_block_success(self):
        outcome = evaluate_outcome(
            self._task(), [self._post("t", "test", False)], 0, [], False
        )
        assert outcome.success is False
        assert "visible tests" in outcome.failure_reason

    def test_regression_blocks_success(self):
        outcome = evaluate_outcome(
            self._task(), [self._post("t", "test", True)], 1, ["other"], False
        )
        assert outcome.success is False
        assert "regression" in outcome.failure_reason

    def test_timeout_blocks_success(self):
        outcome = evaluate_outcome(
            self._task(), [self._post("t", "test", True)], 0, [], True
        )
        assert outcome.success is False
        assert "timed out" in outcome.failure_reason

    def test_agent_error_blocks_success(self):
        outcome = evaluate_outcome(
            self._task(), [self._post("t", "test", True)], 0, [], False, "api error"
        )
        assert outcome.success is False
        assert "api error" in outcome.failure_reason

    def test_hidden_tests_are_gated_separately(self):
        # The task must actually declare a hidden check, otherwise the spec validator
        # correctly softens the gate (see test_hidden_gate_is_softened_when_none_exist).
        task = TaskSpec.model_validate({
            "id": "t", "name": "T", "category": "bug_fix",
            "repository": {"path": "./x"}, "task": {"prompt": "p"},
            "validation": [
                {"name": "visible", "kind": "test", "command": "true"},
                {"name": "hidden", "kind": "test", "command": "true", "hidden": True},
            ],
            "success": {"require_tests_pass": True, "require_hidden_tests_pass": True},
        })
        results = [
            self._post("visible", "test", True),
            self._post("hidden", "test", False, hidden=True),
        ]
        outcome = evaluate_outcome(task, results, 0, [], False)
        assert outcome.tests_passed is True
        assert outcome.hidden_tests_passed is False
        assert outcome.success is False

    def test_lint_gate_only_applies_when_required(self):
        results = [self._post("t", "test", True), self._post("l", "lint", False)]
        assert evaluate_outcome(self._task(), results, 0, [], False).success is True
        gated = self._task(require_tests_pass=True, require_lint_pass=True)
        assert evaluate_outcome(gated, results, 0, [], False).success is False

    def test_pass_rate_prefers_parsed_test_counts(self):
        results = [self._post("t", "test", False, tests_passed=8, tests_failed=2, tests_total=10)]
        outcome = evaluate_outcome(self._task(), results, 0, [], False)
        assert outcome.test_pass_rate == pytest.approx(0.8)

    def test_pass_rate_falls_back_to_command_outcomes(self):
        results = [self._post("a", "test", True), self._post("b", "test", False)]
        outcome = evaluate_outcome(self._task(), results, 0, [], False)
        assert outcome.test_pass_rate == pytest.approx(0.5)


class TestScoreDimensions:
    def test_functional_correctness_rewards_passing_tests(self, sample_task):
        outcome = OutcomeMetrics(
            success=True, test_pass_rate=1.0, hidden_test_pass_rate=1.0, build_passed=True
        )
        assert score_functional_correctness(outcome, sample_task) == pytest.approx(1.0)

    def test_regressions_severely_penalise_correctness(self, sample_task):
        clean = OutcomeMetrics(test_pass_rate=1.0, hidden_test_pass_rate=1.0)
        broken = OutcomeMetrics(test_pass_rate=1.0, hidden_test_pass_rate=1.0, regression_count=1)
        assert score_functional_correctness(broken, sample_task) < 0.51 * (
            score_functional_correctness(clean, sample_task) + 0.01
        )
        assert score_functional_correctness(broken, sample_task) == pytest.approx(0.5)

    def test_many_regressions_drive_correctness_toward_zero(self, sample_task):
        outcome = OutcomeMetrics(test_pass_rate=1.0, regression_count=6)
        assert score_functional_correctness(outcome, sample_task) == 0.0

    def test_efficiency_is_full_within_budget(self, sample_task):
        tools = ToolMetrics(total_calls=10)
        assert score_agent_efficiency(tools, sample_task, ScoringWeights()) == pytest.approx(1.0)

    def test_efficiency_decays_beyond_budget(self, sample_task):
        lean = score_agent_efficiency(ToolMetrics(total_calls=10), sample_task, ScoringWeights())
        bloated = score_agent_efficiency(
            ToolMetrics(total_calls=100), sample_task, ScoringWeights()
        )
        assert bloated < lean

    def test_failed_calls_reduce_efficiency(self, sample_task):
        clean = score_agent_efficiency(ToolMetrics(total_calls=10), sample_task, ScoringWeights())
        messy = score_agent_efficiency(
            ToolMetrics(total_calls=10, failed_calls=3), sample_task, ScoringWeights()
        )
        assert messy < clean

    def test_repeated_reads_reduce_efficiency(self, sample_task):
        clean = score_agent_efficiency(ToolMetrics(total_calls=10), sample_task, ScoringWeights())
        churny = score_agent_efficiency(
            ToolMetrics(total_calls=10, repeated_reads=5), sample_task, ScoringWeights()
        )
        assert churny < clean

    def test_efficiency_is_none_without_any_calls(self, sample_task):
        assert score_agent_efficiency(ToolMetrics(), sample_task, ScoringWeights()) is None

    def test_time_score_is_relative_to_the_timeout(self, sample_task):
        # sample_task has a 1-minute timeout.
        assert score_time(TimeMetrics(wall_clock_ms=0), sample_task) == pytest.approx(1.0)
        assert score_time(TimeMetrics(wall_clock_ms=30_000), sample_task) == pytest.approx(0.5)
        assert score_time(TimeMetrics(wall_clock_ms=60_000), sample_task) == pytest.approx(0.0)
        assert score_time(TimeMetrics(wall_clock_ms=120_000), sample_task) == 0.0

    def test_cost_is_normalised_within_the_comparison(self):
        assert score_cost(CostContext(min_cost=1, max_cost=5, run_cost=1)) == pytest.approx(1.0)
        assert score_cost(CostContext(min_cost=1, max_cost=5, run_cost=5)) == pytest.approx(0.0)
        assert score_cost(CostContext(min_cost=1, max_cost=5, run_cost=3)) == pytest.approx(0.5)

    def test_cost_score_is_none_when_unpriced(self):
        assert score_cost(CostContext()) is None

    def test_identical_costs_all_score_one(self):
        assert score_cost(CostContext(min_cost=2, max_cost=2, run_cost=2)) == pytest.approx(1.0)


class TestCompositeScore:
    def test_perfect_run_scores_one(self, sample_task):
        score = compute_score(
            sample_task,
            OutcomeMetrics(success=True, test_pass_rate=1.0, build_passed=True,
                           lint_passed=True, typecheck_passed=True),
            ToolMetrics(total_calls=5),
            TimeMetrics(wall_clock_ms=0),
            CostContext(min_cost=1, max_cost=5, run_cost=1),
        )
        assert score.composite == pytest.approx(1.0)

    def test_unmeasurable_dimensions_are_dropped_and_weights_renormalised(self, sample_task):
        """A task without a linter must not be penalised for having no lint score."""
        score = compute_score(
            sample_task,
            OutcomeMetrics(success=True, test_pass_rate=1.0),
            ToolMetrics(total_calls=5),
            TimeMetrics(wall_clock_ms=0),
            CostContext(),  # unpriced
        )
        assert score.code_quality is None
        assert score.cost is None
        assert score.composite == pytest.approx(1.0)
        assert any("re-normalised" in n for n in score.notes)

    def test_weights_are_recorded_with_the_score(self, sample_task):
        score = compute_score(
            sample_task, OutcomeMetrics(), ToolMetrics(total_calls=1), TimeMetrics()
        )
        assert score.weights["functional_correctness"] == 0.50

    def test_weights_are_configurable(self, sample_task):
        weights = ScoringWeights(
            functional_correctness=1.0, code_quality=0.0, agent_efficiency=0.0,
            time=0.0, cost=0.0,
        )
        score = compute_score(
            sample_task,
            OutcomeMetrics(test_pass_rate=0.5),
            ToolMetrics(total_calls=1000),
            TimeMetrics(wall_clock_ms=60_000),
            weights=weights,
        )
        # Only correctness carries weight, so the composite equals it.
        assert score.composite == pytest.approx(score.functional_correctness)

    def test_composite_is_bounded(self, sample_task):
        score = compute_score(
            sample_task,
            OutcomeMetrics(test_pass_rate=1.0, regression_count=10),
            ToolMetrics(total_calls=10_000, failed_calls=9_000),
            TimeMetrics(wall_clock_ms=10**9),
            CostContext(min_cost=1, max_cost=2, run_cost=2),
        )
        assert 0.0 <= score.composite <= 1.0

    def test_scoring_config_defaults_when_file_missing(self, tmp_path: Path):
        assert ScoringConfig.load(tmp_path / "nope.yaml").weights.functional_correctness == 0.50

    def test_scoring_config_loads_from_disk(self, tmp_path: Path):
        path = tmp_path / "scoring.yaml"
        path.write_text("weights:\n  functional_correctness: 0.9\n")
        assert ScoringConfig.load(path).weights.functional_correctness == 0.9
