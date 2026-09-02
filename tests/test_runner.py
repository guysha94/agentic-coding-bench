"""Runner behaviour: command construction, environment hygiene, timeouts, orchestration."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from acb.adapters.registry import get_adapter
from acb.eval.scoring import ScoringWeights
from acb.runner.agent import DEFAULT_TOOLS, ClaudeCodeRunner, MockAgentRunner
from acb.runner.env import ENV_ALLOWLIST, build_agent_env, redact, secret_values
from acb.runner.orchestrator import Orchestrator, RunnerConfig


@pytest.fixture
def runner_config(tmp_path: Path) -> RunnerConfig:
    return RunnerConfig(
        repo_root=tmp_path,
        runs_dir=tmp_path / "runs",
        scoring_weights=ScoringWeights(),
        workspace_root=tmp_path / "ws",
    )


class TestCommandConstruction:
    def test_isolation_flags_are_always_present(self, sample_task, anthropic_target, tmp_path):
        cmd = ClaudeCodeRunner().build_command(
            sample_task, anthropic_target, get_adapter(anthropic_target), tmp_path / "mcp.json"
        )
        joined = " ".join(cmd)
        # Each of these was verified necessary to strip ambient developer config.
        assert "--strict-mcp-config" in cmd
        assert "--disable-slash-commands" in cmd
        assert "--setting-sources" in cmd
        assert "--no-session-persistence" in cmd
        assert "--output-format stream-json" in joined
        assert "--verbose" in cmd

    def test_model_comes_from_the_target(self, sample_task, anthropic_target, tmp_path):
        cmd = ClaudeCodeRunner().build_command(
            sample_task, anthropic_target, get_adapter(anthropic_target), None
        )
        assert cmd[cmd.index("--model") + 1] == "opus"

    def test_default_tool_allowlist_is_explicit(self, sample_task, anthropic_target):
        cmd = ClaudeCodeRunner().build_command(
            sample_task, anthropic_target, get_adapter(anthropic_target), None
        )
        assert cmd[cmd.index("--tools") + 1] == ",".join(DEFAULT_TOOLS)

    def test_task_can_override_the_tool_allowlist(self, sample_task, anthropic_target):
        sample_task.required_tools = ["Read", "Bash"]
        cmd = ClaudeCodeRunner().build_command(
            sample_task, anthropic_target, get_adapter(anthropic_target), None
        )
        assert cmd[cmd.index("--tools") + 1] == "Read,Bash"

    def test_command_is_identical_for_two_targets_apart_from_the_model(
        self, sample_task, anthropic_target, openai_target
    ):
        """The core experimental control: only the model/provider may differ."""
        a = ClaudeCodeRunner().build_command(
            sample_task, anthropic_target, get_adapter(anthropic_target), None
        )
        b = ClaudeCodeRunner().build_command(
            sample_task, openai_target, get_adapter(openai_target), None
        )
        a[a.index("--model") + 1] = "<MODEL>"
        b[b.index("--model") + 1] = "<MODEL>"
        # The anthropic target carries an `effort` param; strip provider-declared extras.
        assert [x for x in a if x not in ("--effort", "high")] == b

    def test_adapter_cli_args_are_appended(self, sample_task, anthropic_target):
        anthropic_target.model_params = {"effort": "max"}
        cmd = ClaudeCodeRunner().build_command(
            sample_task, anthropic_target, get_adapter(anthropic_target), None
        )
        assert cmd[-2:] == ["--effort", "max"]

    def test_permission_mode_is_configurable(self, sample_task, anthropic_target):
        cmd = ClaudeCodeRunner(permission_mode="acceptEdits").build_command(
            sample_task, anthropic_target, get_adapter(anthropic_target), None
        )
        assert cmd[cmd.index("--permission-mode") + 1] == "acceptEdits"


class TestEnvironmentHygiene:
    def test_only_allowlisted_variables_are_inherited(self):
        operator = {
            "PATH": "/usr/bin",
            "HOME": "/home/dev",
            "AWS_SECRET_ACCESS_KEY": "prod-secret",
            "DATABASE_URL": "postgres://prod",
            "GITHUB_TOKEN": "ghp_realtoken",
        }
        env = build_agent_env({}, None, operator)
        assert env["PATH"] == "/usr/bin"
        for leaked in ("AWS_SECRET_ACCESS_KEY", "DATABASE_URL", "GITHUB_TOKEN"):
            assert leaked not in env, f"{leaked} leaked into the agent environment"

    def test_provider_credentials_are_injected(self):
        env = build_agent_env({"ANTHROPIC_API_KEY": "k"}, None, {"PATH": "/usr/bin"})
        assert env["ANTHROPIC_API_KEY"] == "k"

    def test_config_dir_is_set_when_isolating(self, tmp_path):
        env = build_agent_env({}, str(tmp_path / "cfg"), {"PATH": "/usr/bin"})
        assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "cfg")

    def test_autoupdate_is_disabled_so_the_version_cannot_drift_mid_suite(self):
        assert build_agent_env({}, None, {})["DISABLE_AUTOUPDATER"] == "1"

    def test_allowlist_covers_common_build_tooling(self):
        for needed in ("PATH", "HOME", "LANG", "TMPDIR"):
            assert needed in ENV_ALLOWLIST


class TestRedaction:
    def test_secret_values_are_identified_by_name(self):
        env = {"ANTHROPIC_API_KEY": "supersecretvalue", "PATH": "/usr/bin"}
        assert secret_values(env) == ["supersecretvalue"]

    def test_known_values_are_redacted_from_text(self):
        text = "calling api with key supersecretvalue now"
        assert "supersecretvalue" not in redact(text, ["supersecretvalue"])
        assert "[REDACTED]" in redact(text, ["supersecretvalue"])

    def test_secret_shaped_strings_are_redacted_without_being_known(self):
        for secret in (
            "sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAA",
            "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            "AKIAIOSFODNN7EXAMPLE",
        ):
            assert secret not in redact(f"token: {secret}", [])

    def test_redaction_handles_empty_input(self):
        assert redact("", ["x"]) == ""

    def test_short_values_are_not_treated_as_secrets(self):
        assert secret_values({"API_KEY": "abc"}) == []


class TestMockRunner:
    def test_replays_events_and_mutates_the_workspace(
        self, sample_task, anthropic_target, tmp_path, trajectory_events
    ):
        workspace = tmp_path / "ws"
        workspace.mkdir()
        runner = MockAgentRunner(
            events=trajectory_events, file_changes={"calc.py": "def add(a, b):\n    return a + b\n"}
        )
        result = runner.run(
            sample_task, anthropic_target, get_adapter(anthropic_target),
            workspace, "prompt", None, None,
        )
        assert result.exit_code == 0
        assert len(result.stdout_lines) == len(trajectory_events)
        assert (workspace / "calc.py").read_text().endswith("a + b\n")
        assert runner.calls[0]["task"] == sample_task.id


class TestOrchestration:
    def _orchestrator(self, config, runner):
        return Orchestrator(config, agent_runner=runner)

    def test_successful_run_end_to_end(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        runner = MockAgentRunner(
            events=trajectory_events, file_changes={"calc.py": "def add(a, b):\n    return a + b\n"}
        )
        record = self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        assert record.outcome == "success"
        assert record.result.success is True
        assert record.result.files_changed == 1
        assert record.tools.total_calls == 6
        assert record.usage.input_tokens == 265
        assert record.cost.total_cost is not None
        assert 0.0 < record.score.composite <= 1.0

    def test_failed_run_is_reported_with_a_reason(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        # The agent changes nothing, so the failing test stays failing.
        runner = MockAgentRunner(events=trajectory_events)
        record = self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        assert record.outcome == "failure"
        assert record.result.success is False
        assert "tests" in (record.result.failure_reason or "")

    def test_regression_is_detected_against_the_baseline(
        self, git_fixture_repo, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        """A check that passed before the agent ran and fails after is a regression."""
        from acb.models.task import TaskSpec

        task = TaskSpec.model_validate({
            "id": "reg-task", "name": "R", "category": "refactor",
            "repository": {"path": str(git_fixture_repo)},
            "task": {"prompt": "p"}, "timeout_minutes": 1,
            "validation": [{"name": "always-green", "kind": "test",
                            "command": "test -f marker.txt"}],
        })
        # Baseline passes because the agent has not run yet...
        (git_fixture_repo / "marker.txt").write_text("x")
        import subprocess
        subprocess.run(["git", "-C", str(git_fixture_repo), "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", str(git_fixture_repo), "commit", "-q", "-m", "marker"],
                       check=True, capture_output=True)

        # ...and the agent deletes the marker, breaking it.
        class Deleter(MockAgentRunner):
            def run(self, task, target, adapter, workspace, prompt, config_dir, mcp_config):
                (workspace / "marker.txt").unlink()
                return super().run(task, target, adapter, workspace, prompt, config_dir,
                                   mcp_config)

        record = self._orchestrator(runner_config, Deleter(events=trajectory_events)).run_once(
            task, anthropic_target, tmp_path
        )
        assert record.result.regression_count == 1
        assert "always-green" in record.result.regressions
        assert record.result.success is False

    def test_timeout_is_recorded_and_artifacts_preserved(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        runner = MockAgentRunner(events=trajectory_events, timed_out=True, exit_code=-15)
        record = self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        assert record.outcome == "timeout"
        assert record.timed_out is True
        assert "timed out" in (record.result.failure_reason or "")
        assert (Path(record.artifacts_dir) / "metadata.json").exists()

    def test_agent_error_is_recorded(
        self, sample_task, anthropic_target, runner_config, tmp_path
    ):
        runner = MockAgentRunner(error="claude not found on PATH", exit_code=None)
        record = self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        assert record.outcome == "error"
        assert "not found" in record.error

    def test_setup_failure_short_circuits_the_run(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        sample_task.repository.setup_commands = ["exit 3"]
        runner = MockAgentRunner(events=trajectory_events)
        record = self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        assert record.outcome == "setup_error"
        assert "setup command failed" in record.error
        assert runner.calls == [], "the agent must not run when setup failed"

    def test_all_required_artifacts_are_written(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        runner = MockAgentRunner(events=trajectory_events, file_changes={"calc.py": "x=1\n"})
        record = self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        d = Path(record.artifacts_dir)
        for name in (
            "metadata.json", "prompt.txt", "agent.log", "stdout.log", "stderr.log",
            "tool_calls.jsonl", "model_usage.json", "git.diff", "validation.json",
            "score.json", "pricing_snapshot.json", "usage/normalized.json",
            "usage/provider_raw.json",
        ):
            assert (d / name).exists(), f"missing required artifact: {name}"

    def test_raw_usage_is_preserved_verbatim(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        """Normalisation can be corrected later only if raw usage is never discarded."""
        runner = MockAgentRunner(events=trajectory_events)
        record = self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        raw = json.loads((Path(record.artifacts_dir) / "usage/provider_raw.json").read_text())
        assert len(raw["payloads"]) == 6
        assert raw["payloads"][0]["cache_creation_input_tokens"] == 5000
        assert raw["result_event"]["total_cost_usd"] == 0.42

    def test_reported_cost_is_kept_but_not_used_for_comparison(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        runner = MockAgentRunner(events=trajectory_events)
        record = self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        assert record.reported_cost_usd == 0.42
        # Our own engine computed something different from the first-party table.
        assert record.cost.total_cost != record.reported_cost_usd

    def test_pricing_snapshot_is_persisted_with_the_run(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        runner = MockAgentRunner(events=trajectory_events)
        record = self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        snap = json.loads((Path(record.artifacts_dir) / "pricing_snapshot.json").read_text())
        assert snap["pricing_snapshot"]["input_per_million_tokens"] == 3.0
        assert snap["normalization_version"]

    def test_environment_fingerprint_is_captured(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        runner = MockAgentRunner(events=trajectory_events)
        record = self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        env = record.environment
        assert env.os and env.arch and env.python_version and env.framework_version
        assert env.fixture_commit, "the exact commit the agent started from must be recorded"
        assert env.mcp_fingerprint
        assert env.session_id == "sess-1"
        assert env.model_reported == "claude-opus-5"

    def test_workspace_is_destroyed_after_the_run(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        runner = MockAgentRunner(events=trajectory_events)
        self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        leftovers = list((tmp_path / "ws").iterdir()) if (tmp_path / "ws").exists() else []
        assert leftovers == []

    def test_workspace_kept_when_requested(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        runner_config.keep_workspace = True
        runner = MockAgentRunner(events=trajectory_events)
        record = self._orchestrator(runner_config, runner).run_once(
            sample_task, anthropic_target, tmp_path
        )
        assert any("workspace kept" in n for n in record.environment.notes)

    def test_prompt_is_identical_across_targets(
        self, sample_task, anthropic_target, openai_target, runner_config, tmp_path,
        trajectory_events
    ):
        """The task instruction must be byte-identical, or the experiment is invalid."""
        a = MockAgentRunner(events=trajectory_events)
        b = MockAgentRunner(events=trajectory_events)
        orch = self._orchestrator(runner_config, a)
        orch.run_once(sample_task, anthropic_target, tmp_path)
        orch.agent_runner = b
        orch.run_once(sample_task, openai_target, tmp_path)
        assert a.calls[0]["prompt"] == b.calls[0]["prompt"]

    def test_run_ids_are_unique(
        self, sample_task, anthropic_target, runner_config, tmp_path, trajectory_events
    ):
        orch = self._orchestrator(runner_config, MockAgentRunner(events=trajectory_events))
        ids = {
            orch.run_once(sample_task, anthropic_target, tmp_path, repetition=i).run_id
            for i in range(3)
        }
        assert len(ids) == 3

    def test_empty_mcp_config_is_written_when_a_task_declares_none(
        self, sample_task, anthropic_target, runner_config
    ):
        path = self._orchestrator(
            runner_config, MockAgentRunner()
        ).mcp_config_path(sample_task)
        assert json.loads(path.read_text()) == {"mcpServers": {}}

    def test_mcp_fingerprint_reflects_the_server_set(self, runner_config, tmp_path):
        orch = self._orchestrator(runner_config, MockAgentRunner())
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        a.write_text(json.dumps({"mcpServers": {"specs": {"command": "x"}}}))
        b.write_text(json.dumps({"mcpServers": {"other": {"command": "y"}}}))
        assert orch.mcp_fingerprint(a) != orch.mcp_fingerprint(b)
        assert orch.mcp_fingerprint(a) == orch.mcp_fingerprint(a)


class TestRealRunnerGuards:
    def test_missing_executable_is_reported_not_raised(
        self, sample_task, anthropic_target, tmp_path
    ):
        runner = ClaudeCodeRunner(executable="definitely-not-a-real-binary-xyz")
        result = runner.run(
            sample_task, anthropic_target, get_adapter(anthropic_target),
            tmp_path, "prompt", None, None,
        )
        assert result.error is not None
        assert "not found on PATH" in result.error

    @pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")
    def test_timeout_terminates_the_process_group(self, sample_task, anthropic_target, tmp_path):
        """A hung agent must be killed along with any children it spawned."""
        import time

        sample_task.timeout_minutes = 1
        runner = ClaudeCodeRunner(executable="sleep")
        # Build a command by hand: we only need the timeout machinery here.
        original = runner.build_command
        runner.build_command = lambda *a, **k: ["sleep", "30"]  # type: ignore[assignment]
        sample_task.timeout_minutes = 0  # 0s timeout -> immediate expiry
        object.__setattr__(sample_task, "timeout_minutes", 0)

        started = time.monotonic()
        result = runner.run(
            sample_task, anthropic_target, get_adapter(anthropic_target),
            tmp_path, "prompt", None, None,
        )
        elapsed = time.monotonic() - started
        runner.build_command = original  # type: ignore[assignment]
        assert result.timed_out is True
        assert elapsed < 25, "the process should have been killed well before it finished"
