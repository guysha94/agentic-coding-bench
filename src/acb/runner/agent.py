"""Agent runners.

`AgentRunner` is the seam between the benchmark and the thing being measured. The real
implementation drives `claude -p` as a subprocess; `MockAgentRunner` replays scripted
trajectories so the framework's own test suite never spends money or needs a network.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from acb.adapters.base import ProviderAdapter
from acb.models.target import TargetConfig
from acb.models.task import TaskSpec
from acb.runner.env import build_agent_env, redact, secret_values

# Default tool allowlist. Explicit rather than inherited, so "same available tools" is
# actually true across targets and machines (see design doc Sec 4.3).
DEFAULT_TOOLS = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "Glob",
    "Grep",
    "TodoWrite",
]

GRACE_PERIOD_SECONDS = 10


@dataclass
class AgentResult:
    """Raw outcome of one agent session, before any interpretation."""

    stdout_lines: list[str] = field(default_factory=list)
    stderr: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    wall_clock_ms: int = 0
    command: list[str] = field(default_factory=list)
    error: str | None = None


class AgentRunner(ABC):
    @abstractmethod
    def run(
        self,
        task: TaskSpec,
        target: TargetConfig,
        adapter: ProviderAdapter,
        workspace: Path,
        prompt: str,
        config_dir: Path | None,
        mcp_config: Path | None,
    ) -> AgentResult: ...


class ClaudeCodeRunner(AgentRunner):
    """Drives the real `claude` CLI in headless streaming mode.

    Flag choices are deliberate and verified (design doc Sec 4.3/4.4):

    * `--output-format stream-json --verbose` -- the only way to get tool calls and usage.
    * `--strict-mcp-config --mcp-config` -- pins MCP to the benchmark's config.
    * `--setting-sources ""` -- ignores user/project/local settings.
    * `--disable-slash-commands` -- removes ambient skills.
    * `--tools <explicit>` -- makes the tool set an experimental parameter.
    * `CLAUDE_CONFIG_DIR=<scratch>` -- strips plugins/agents/skills and prior session state.
    """

    def __init__(self, executable: str = "claude", permission_mode: str = "bypassPermissions"):
        self.executable = executable
        self.permission_mode = permission_mode

    def build_command(
        self,
        task: TaskSpec,
        target: TargetConfig,
        adapter: ProviderAdapter,
        mcp_config: Path | None,
        session_id: str | None = None,
    ) -> list[str]:
        tools = task.required_tools if task.required_tools is not None else DEFAULT_TOOLS
        cmd = [
            self.executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            target.model,
            "--permission-mode",
            self.permission_mode,
            "--tools",
            ",".join(tools),
            "--setting-sources",
            "",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        if mcp_config is not None:
            cmd += ["--mcp-config", str(mcp_config)]
        if session_id:
            cmd += ["--session-id", session_id]
        cmd += adapter.cli_args()
        return cmd

    def run(
        self,
        task: TaskSpec,
        target: TargetConfig,
        adapter: ProviderAdapter,
        workspace: Path,
        prompt: str,
        config_dir: Path | None,
        mcp_config: Path | None,
    ) -> AgentResult:
        if shutil.which(self.executable) is None:
            return AgentResult(
                error=f"`{self.executable}` not found on PATH; run `benchmark doctor`",
                exit_code=None,
            )

        operator_env = dict(os.environ)
        provider_env = adapter.agent_env(operator_env)
        env = build_agent_env(
            provider_env,
            str(config_dir) if (config_dir and target.isolate_config_dir) else None,
            operator_env,
        )
        secrets = secret_values(provider_env)
        cmd = self.build_command(task, target, adapter, mcp_config)

        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(workspace),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # New process group so a timeout kills the agent's children (test runners,
                # dev servers) too, not just the CLI itself.
                start_new_session=True,
            )
        except OSError as exc:
            return AgentResult(error=f"failed to start agent: {exc}", command=cmd)

        timed_out = False
        try:
            stdout, stderr = proc.communicate(input=prompt, timeout=task.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            stdout, stderr = self._terminate(proc)
        finally:
            wall_clock_ms = int((time.monotonic() - started) * 1000)

        return AgentResult(
            stdout_lines=(stdout or "").splitlines(),
            stderr=redact(stderr or "", secrets),
            exit_code=proc.returncode,
            timed_out=timed_out,
            wall_clock_ms=wall_clock_ms,
            command=cmd,
        )

    @staticmethod
    def _terminate(proc: subprocess.Popen[str]) -> tuple[str, str]:
        """SIGTERM the whole process group, then SIGKILL. Always returns partial output."""
        for sig, wait in ((signal.SIGTERM, GRACE_PERIOD_SECONDS), (signal.SIGKILL, 5)):
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError):
                break
            try:
                return proc.communicate(timeout=wait)
            except subprocess.TimeoutExpired:
                continue
        try:
            return proc.communicate(timeout=5)
        except Exception:
            return "", "agent did not exit after SIGKILL; output may be truncated"


class MockAgentRunner(AgentRunner):
    """Replays a scripted stream-json trajectory, and optionally mutates the workspace.

    This is what makes the framework's own test suite fast, deterministic and free.
    """

    def __init__(
        self,
        events: list[dict] | None = None,
        file_changes: dict[str, str] | None = None,
        exit_code: int = 0,
        timed_out: bool = False,
        wall_clock_ms: int = 1234,
        stderr: str = "",
        error: str | None = None,
    ) -> None:
        self.events = events or []
        self.file_changes = file_changes or {}
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.wall_clock_ms = wall_clock_ms
        self.stderr = stderr
        self.error = error
        self.calls: list[dict] = []

    def run(
        self,
        task: TaskSpec,
        target: TargetConfig,
        adapter: ProviderAdapter,
        workspace: Path,
        prompt: str,
        config_dir: Path | None,
        mcp_config: Path | None,
    ) -> AgentResult:
        self.calls.append(
            {"task": task.id, "target": target.id, "workspace": str(workspace), "prompt": prompt}
        )
        for rel, content in self.file_changes.items():
            dest = workspace / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content)
        return AgentResult(
            stdout_lines=[json.dumps(e) for e in self.events],
            stderr=self.stderr,
            exit_code=self.exit_code,
            timed_out=self.timed_out,
            wall_clock_ms=self.wall_clock_ms,
            error=self.error,
            command=["mock-agent"],
        )
