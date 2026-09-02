"""Run orchestration -- the 10-step protocol from the design doc, Sec 5.2.

    1.  freeze a RunSpec (task + target + pricing)
    2.  create an isolated workspace at the exact base commit
    3.  assert clean; record the environment fingerprint
    4.  baseline validation  (so regressions can be distinguished from pre-existing failures)
    5.  execute the agent, streaming NDJSON to disk
    6.  on timeout: terminate, keep partial artifacts
    7.  capture git diff
    8.  post validation (visible, then hidden)
    9.  normalise usage -> cost -> score
    10. persist artifacts + DB row; destroy the workspace

Artifacts are written incrementally so a crashed or killed run still leaves evidence.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from acb import __version__
from acb.adapters.registry import get_adapter
from acb.cost.engine import compute_cost
from acb.eval.scoring import CostContext, ScoringWeights, compute_score, evaluate_outcome
from acb.eval.validation import detect_regressions, run_validation
from acb.models.run import EnvironmentInfo, RunRecord, TimeMetrics, utcnow
from acb.models.target import TargetConfig
from acb.models.task import TaskSpec
from acb.models.usage import RawUsageRecord
from acb.runner.agent import AgentRunner, ClaudeCodeRunner
from acb.runner.env import redact, secret_values
from acb.runner.telemetry import build_tool_metrics, parse_stream
from acb.runner.workspace import WorkspaceError, create_workspace, resolve_commit


def new_run_id(task_id: str, target_id: str, repetition: int) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{stamp}-{task_id}-{target_id}-r{repetition}-{uuid.uuid4().hex[:6]}"


@dataclass
class RunnerConfig:
    repo_root: Path
    runs_dir: Path
    scoring_weights: ScoringWeights
    workspace_root: Path | None = None
    permission_mode: str = "bypassPermissions"
    claude_executable: str = "claude"
    keep_workspace: bool = False


class Orchestrator:
    def __init__(self, config: RunnerConfig, agent_runner: AgentRunner | None = None) -> None:
        self.config = config
        self.agent_runner = agent_runner or ClaudeCodeRunner(
            executable=config.claude_executable, permission_mode=config.permission_mode
        )

    # ------------------------------------------------------------------ prompt ---------
    def build_prompt(self, task: TaskSpec, workspace: Path) -> str:
        """Byte-identical instruction for every target.

        Context files are read from the workspace so the prompt is identical across runs
        of the same base commit.
        """
        parts = [task.task.prompt.strip()]
        for rel in task.task.context_files:
            path = workspace / rel
            if path.exists():
                parts.append(f"\n--- {rel} ---\n{path.read_text()}")
            else:
                parts.append(f"\n--- {rel} ---\n[file not found in workspace]")
        return "\n".join(parts)

    def mcp_config_path(self, task: TaskSpec) -> Path:
        """MCP config for this task. An empty config is written when none is declared, so
        `--strict-mcp-config` yields exactly zero servers rather than inheriting any."""
        if task.mcp and task.mcp.config_file:
            path = (self.config.repo_root / task.mcp.config_file).resolve()
            if not path.exists():
                raise WorkspaceError(f"task {task.id}: MCP config not found: {path}")
            return path
        default = self.config.runs_dir / "_empty_mcp.json"
        default.parent.mkdir(parents=True, exist_ok=True)
        if not default.exists():
            default.write_text(json.dumps({"mcpServers": {}}))
        return default

    @staticmethod
    def mcp_fingerprint(path: Path) -> str:
        try:
            payload = json.loads(path.read_text())
            servers = sorted((payload.get("mcpServers") or {}).keys())
            body = json.dumps({"servers": servers}, sort_keys=True)
        except Exception:
            body = path.read_text() if path.exists() else ""
        return hashlib.sha256(body.encode()).hexdigest()[:16]

    # ------------------------------------------------------------------ main entry -----
    def run_once(
        self,
        task: TaskSpec,
        target: TargetConfig,
        task_dir: Path,
        repetition: int = 1,
        batch_id: str | None = None,
        cost_ctx: CostContext | None = None,
    ) -> RunRecord:
        run_id = new_run_id(task.id, target.id, repetition)
        artifacts = self.config.runs_dir / run_id
        artifacts.mkdir(parents=True, exist_ok=True)

        adapter = get_adapter(target)
        record = RunRecord(
            run_id=run_id,
            task_id=task.id,
            task_version=task.version,
            task_category=task.category,
            task_difficulty=task.difficulty,
            target_id=target.id,
            model=target.model,
            model_family=target.model_family,
            provider=target.provider,
            deployment_type=target.deployment_type,
            repetition=repetition,
            batch_id=batch_id,
            artifacts_dir=str(artifacts),
            pricing_snapshot=target.pricing_snapshot(),
        )
        record.environment = EnvironmentInfo.collect(__version__)
        _write(artifacts / "pricing_snapshot.json", json.dumps(record.pricing_snapshot, indent=2))

        secrets = secret_values(adapter.agent_env(_operator_env()))
        workspace = None
        timings = TimeMetrics()

        try:
            # --- 2/3: isolated workspace at the exact commit ---------------------------
            repo_path = (self.config.repo_root / task.repository.path).resolve()
            record.environment.fixture_commit = resolve_commit(
                repo_path, task.repository.base_commit
            )
            workspace = create_workspace(
                repo_path,
                task.repository.base_commit,
                mode=task.repository.isolation,
                root=self.config.workspace_root,
                label=task.id,
            )

            setup_started = time.monotonic()
            setup_error = self._run_setup(task, workspace.path, artifacts)
            timings.setup_ms = int((time.monotonic() - setup_started) * 1000)
            if setup_error:
                record.outcome = "setup_error"
                record.error = setup_error
                record.time = timings
                record.result.failure_reason = setup_error
                record.finished_at = utcnow()
                return record

            mcp_config = self.mcp_config_path(task)
            record.environment.mcp_fingerprint = self.mcp_fingerprint(mcp_config)
            record.environment.permission_mode = self.config.permission_mode

            prompt = self.build_prompt(task, workspace.path)
            _write(artifacts / "prompt.txt", prompt)

            # --- 4: baseline validation ----------------------------------------------
            validation_started = time.monotonic()
            baseline = run_validation(task, workspace.path, task_dir, "baseline", secrets)
            record.validations.extend(baseline)

            # --- 5/6: execute the agent ----------------------------------------------
            config_dir = artifacts / "claude_config"
            config_dir.mkdir(parents=True, exist_ok=True)
            agent_result = self.agent_runner.run(
                task=task,
                target=target,
                adapter=adapter,
                workspace=workspace.path,
                prompt=prompt,
                config_dir=config_dir,
                mcp_config=mcp_config,
            )

            _write(
                artifacts / "agent.log",
                redact("\n".join(agent_result.stdout_lines), secrets),
            )
            _write(artifacts / "stdout.log", redact("\n".join(agent_result.stdout_lines), secrets))
            _write(artifacts / "stderr.log", redact(agent_result.stderr, secrets))
            _write(
                artifacts / "command.json",
                json.dumps({"command": agent_result.command, "cwd": str(workspace.path)}, indent=2),
            )

            record.agent_exit_code = agent_result.exit_code
            record.timed_out = agent_result.timed_out
            timings.wall_clock_ms = agent_result.wall_clock_ms

            # --- telemetry -------------------------------------------------------------
            traj = parse_stream(agent_result.stdout_lines)
            _write_jsonl(
                artifacts / "tool_calls.jsonl",
                [ev.model_dump(mode="json") for ev in traj.tool_events],
            )
            if traj.parse_errors:
                _write(artifacts / "telemetry_errors.log", "\n".join(traj.parse_errors))

            record.tools = build_tool_metrics(traj, task.mcp)
            record.num_turns = traj.num_turns
            record.permission_denials = traj.permission_denials
            record.terminal_reason = traj.terminal_reason
            record.environment.session_id = traj.session_id
            record.environment.model_reported = traj.model_reported
            record.environment.mcp_servers = traj.mcp_servers()
            record.environment.tools_available = traj.tools_available()
            if traj.init_event and traj.init_event.get("claude_code_version"):
                record.environment.claude_code_version = traj.init_event["claude_code_version"]
            record.reported_cost_usd = traj.reported_cost_usd

            # Prefer the CLI's own timing when available: it excludes our process overhead.
            if traj.wall_clock_ms and not agent_result.timed_out:
                timings.wall_clock_ms = traj.wall_clock_ms
            timings.api_ms = traj.api_ms
            timings.ttft_ms = traj.ttft_ms
            timings.time_to_first_edit_ms = traj.time_to_first_edit_ms()

            # --- usage + cost ----------------------------------------------------------
            record.usage = adapter.normalize_usage(traj.usage_payloads)
            raw = RawUsageRecord(
                provider=target.provider,
                model=target.model,
                payloads=traj.usage_payloads,
                result_event=traj.result_event,
            )
            usage_dir = artifacts / "usage"
            usage_dir.mkdir(exist_ok=True)
            _write(
                usage_dir / "normalized.json",
                json.dumps(record.usage.model_dump(mode="json"), indent=2),
            )
            _write(
                usage_dir / "provider_raw.json",
                redact(json.dumps(raw.model_dump(mode="json"), indent=2), secrets),
            )

            # --- 7: capture the diff ---------------------------------------------------
            try:
                diff = workspace.diff()
                _write(artifacts / "git.diff", redact(diff, secrets))
                files, added, removed = workspace.diff_stat()
                changed = workspace.changed_files()
                record.result.files_changed = files
                record.result.lines_added = added
                record.result.lines_removed = removed
                if task.expected_files:
                    record.result.touched_expected_files = len(
                        set(changed) & set(task.expected_files)
                    )
                _write(artifacts / "changed_files.json", json.dumps(changed, indent=2))
            except WorkspaceError as exc:
                record.environment.notes.append(f"diff capture failed: {exc}")

            # --- 8: post validation ----------------------------------------------------
            post = run_validation(task, workspace.path, task_dir, "post", secrets)
            record.validations.extend(post)
            timings.validation_ms = int((time.monotonic() - validation_started) * 1000)

            regression_count, regressions = detect_regressions(baseline, post)

            # --- 9: outcome, score ------------------------------------------------------
            agent_error = agent_result.error or (
                traj.result_text if traj.is_error and traj.result_text else None
            )
            record.result = _merge_outcome(
                record.result,
                evaluate_outcome(
                    task, record.validations, regression_count, regressions,
                    agent_result.timed_out, agent_error,
                ),
            )
            record.error = agent_result.error
            record.time = timings

            runtime_seconds = timings.wall_clock_ms / 1000.0
            record.cost = compute_cost(record.usage, target.pricing, runtime_seconds)
            ctx = cost_ctx or CostContext()
            ctx = CostContext(
                min_cost=ctx.min_cost, max_cost=ctx.max_cost, run_cost=record.cost.total_cost
            )
            record.score = compute_score(
                task, record.result, record.tools, timings, ctx, self.config.scoring_weights
            )

            if agent_result.timed_out:
                record.outcome = "timeout"
            elif agent_result.error:
                record.outcome = "error"
            else:
                record.outcome = "success" if record.result.success else "failure"

        except WorkspaceError as exc:
            record.outcome = "setup_error"
            record.error = str(exc)
            record.result.failure_reason = str(exc)
        except Exception as exc:
            record.outcome = "error"
            record.error = f"{type(exc).__name__}: {exc}"
            record.result.failure_reason = record.error
        finally:
            record.finished_at = utcnow()
            if workspace is not None and not self.config.keep_workspace:
                workspace.cleanup()
            elif workspace is not None:
                record.environment.notes.append(f"workspace kept at {workspace.path}")

            _write(
                artifacts / "metadata.json",
                json.dumps(record.model_dump(mode="json"), indent=2),
            )
            _write(
                artifacts / "validation.json",
                json.dumps([v.model_dump(mode="json") for v in record.validations], indent=2),
            )
            _write(
                artifacts / "score.json", json.dumps(record.score.model_dump(mode="json"), indent=2)
            )
            _write(
                artifacts / "model_usage.json",
                json.dumps(
                    {
                        "normalized": record.usage.model_dump(mode="json"),
                        "cost": record.cost.model_dump(mode="json"),
                        "reported_cost_usd_first_party_table": record.reported_cost_usd,
                    },
                    indent=2,
                ),
            )
        return record

    def _run_setup(self, task: TaskSpec, workspace: Path, artifacts: Path) -> str | None:
        """Run the task's setup commands. Returns an error message on failure."""
        import subprocess

        from acb.eval.validation import _validation_env

        log: list[str] = []
        for cmd in task.repository.setup_commands:
            try:
                proc = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(workspace),
                    env=_validation_env(),
                    capture_output=True,
                    text=True,
                    timeout=900,
                )
            except subprocess.TimeoutExpired:
                return f"setup command timed out: {cmd}"
            except OSError as exc:
                return f"setup command failed to start: {cmd} ({exc})"
            log.append(f"$ {cmd}\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}")
            if proc.returncode != 0:
                _write(artifacts / "setup.log", "\n\n".join(log))
                return f"setup command failed ({proc.returncode}): {cmd}"
        if log:
            _write(artifacts / "setup.log", "\n\n".join(log))
        return None


def _merge_outcome(existing, evaluated):
    """Keep diff statistics gathered before evaluation, take the rest from evaluation."""
    evaluated.files_changed = existing.files_changed
    evaluated.lines_added = existing.lines_added
    evaluated.lines_removed = existing.lines_removed
    evaluated.touched_expected_files = existing.touched_expected_files
    return evaluated


def _operator_env() -> dict[str, str]:
    import os

    return dict(os.environ)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, default=str) for r in rows))
