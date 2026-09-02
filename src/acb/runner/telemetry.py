"""Parse Claude Code's `--output-format stream-json` trajectory.

Everything here is grounded in the event shapes verified against `claude 2.1.252`; see
docs/benchmark-design.md Sec 4.1. Parsing is deliberately defensive: an unrecognised or
malformed event is recorded and skipped rather than aborting a run that may have taken
twenty minutes.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from acb.models.run import ToolEvent, ToolMetrics

# Tool name -> normalised family. Prefix matching handles MCP tools (`mcp__server__tool`).
_TOOL_KINDS: dict[str, str] = {
    "bash": "bash",
    "bashoutput": "bash",
    "killshell": "bash",
    "read": "read",
    "notebookread": "read",
    "write": "write",
    "edit": "edit",
    "multiedit": "edit",
    "notebookedit": "edit",
    "grep": "search",
    "glob": "search",
    "ls": "search",
    "webfetch": "web",
    "websearch": "web",
    "task": "subagent",
    "agent": "subagent",
    "todowrite": "planning",
    "exitplanmode": "planning",
    "enterplanmode": "planning",
}

_MUTATING_KINDS = {"write", "edit"}
# Bash commands that constitute a code modification for time-to-first-edit.
_MUTATING_BASH = re.compile(
    r"\b(?:sed\s+-i|tee\b|patch\b|>>?\s*\S|mv\b|cp\b|rm\b|touch\b|mkdir\b|git\s+(?:apply|checkout|revert|am))",
)
_GIT_BASH = re.compile(r"(?:^|[;&|]\s*)git\s")


def classify_tool(name: str) -> str:
    lowered = name.lower()
    if lowered.startswith("mcp__"):
        return "mcp"
    return _TOOL_KINDS.get(lowered, "other")


def mcp_server_of(name: str) -> str | None:
    if not name.lower().startswith("mcp__"):
        return None
    parts = name.split("__")
    return parts[1] if len(parts) >= 2 else None


def _digest(name: str, tool_input: Any) -> str:
    try:
        payload = json.dumps(tool_input, sort_keys=True, default=str)
    except Exception:
        payload = str(tool_input)
    return hashlib.sha256(f"{name}:{payload}".encode()).hexdigest()[:16]


def _summarize_input(name: str, tool_input: Any) -> str:
    if not isinstance(tool_input, dict):
        return str(tool_input)[:200]
    for key in ("file_path", "path", "command", "pattern", "query", "prompt", "url"):
        if key in tool_input:
            return f"{key}={str(tool_input[key])[:200]}"
    return json.dumps(tool_input, default=str)[:200]


def _target_path(name: str, tool_input: Any) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "notebook_path", "path"):
        v = tool_input.get(key)
        if isinstance(v, str):
            return v
    return None


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class Trajectory:
    """Everything we extract from one agent session."""

    events: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[ToolEvent] = field(default_factory=list)
    usage_payloads: list[dict[str, Any]] = field(default_factory=list)
    init_event: dict[str, Any] | None = None
    result_event: dict[str, Any] | None = None
    session_id: str | None = None
    model_reported: str | None = None
    parse_errors: list[str] = field(default_factory=list)
    session_start: datetime | None = None
    first_edit_at: datetime | None = None
    assistant_text: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ derived --------
    @property
    def num_turns(self) -> int:
        if self.result_event:
            n = self.result_event.get("num_turns")
            if isinstance(n, int):
                return n
        return sum(1 for e in self.events if e.get("type") == "assistant")

    @property
    def wall_clock_ms(self) -> int | None:
        return (self.result_event or {}).get("duration_ms")

    @property
    def api_ms(self) -> int | None:
        return (self.result_event or {}).get("duration_api_ms")

    @property
    def ttft_ms(self) -> int | None:
        return (self.result_event or {}).get("ttft_ms")

    @property
    def reported_cost_usd(self) -> float | None:
        return (self.result_event or {}).get("total_cost_usd")

    @property
    def terminal_reason(self) -> str | None:
        return (self.result_event or {}).get("terminal_reason")

    @property
    def is_error(self) -> bool:
        return bool((self.result_event or {}).get("is_error"))

    @property
    def result_text(self) -> str:
        return str((self.result_event or {}).get("result") or "")

    @property
    def permission_denials(self) -> int:
        denials = (self.result_event or {}).get("permission_denials") or []
        return len(denials) if isinstance(denials, list) else 0

    def time_to_first_edit_ms(self) -> int | None:
        if self.session_start and self.first_edit_at:
            return max(0, int((self.first_edit_at - self.session_start).total_seconds() * 1000))
        return None

    def mcp_servers(self) -> list[dict[str, Any]]:
        servers = (self.init_event or {}).get("mcp_servers") or []
        return servers if isinstance(servers, list) else []

    def tools_available(self) -> list[str]:
        tools = (self.init_event or {}).get("tools") or []
        return tools if isinstance(tools, list) else []


def parse_stream(lines: list[str] | str) -> Trajectory:
    """Parse NDJSON stream-json output into a Trajectory."""
    if isinstance(lines, str):
        lines = lines.splitlines()

    traj = Trajectory()
    pending: dict[str, ToolEvent] = {}
    # Side table: tool_use_id -> timestamp, for calls that would count as a code
    # modification *if they succeed*. Kept beside the model rather than stuffed into it,
    # so ToolEvent stays a clean serialisable record.
    mutating_at: dict[str, datetime] = {}
    seen_digests: set[str] = set()
    index = 0

    for lineno, raw in enumerate(lines, start=1):
        raw = raw.strip()
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError as exc:
            traj.parse_errors.append(f"line {lineno}: {exc}")
            continue
        if not isinstance(event, dict):
            traj.parse_errors.append(f"line {lineno}: event is not an object")
            continue

        traj.events.append(event)
        etype = event.get("type")

        if etype == "system" and event.get("subtype") == "init":
            traj.init_event = event
            traj.session_id = event.get("session_id")
            traj.model_reported = event.get("model")

        elif etype == "assistant":
            message = event.get("message") or {}
            ts = _parse_ts(event.get("timestamp"))
            if ts and traj.session_start is None:
                traj.session_start = ts
            if message.get("model"):
                traj.model_reported = message["model"]

            usage = message.get("usage")
            if isinstance(usage, dict):
                traj.usage_payloads.append(usage)

            for block in message.get("content") or []:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    traj.assistant_text.append(str(block["text"]))
                elif btype == "tool_use":
                    name = str(block.get("name") or "unknown")
                    tool_input = block.get("input")
                    digest = _digest(name, tool_input)
                    kind = classify_tool(name)

                    if kind == "bash" and isinstance(tool_input, dict):
                        cmd = str(tool_input.get("command") or "")
                        if _GIT_BASH.search(cmd):
                            kind = "git"

                    ev = ToolEvent(
                        index=index,
                        tool_use_id=block.get("id"),
                        name=name,
                        kind=kind,
                        input_digest=digest,
                        input_summary=_summarize_input(name, tool_input),
                        target_path=_target_path(name, tool_input),
                        started_at=event.get("timestamp"),
                        is_repeat=digest in seen_digests,
                        parent_tool_use_id=event.get("parent_tool_use_id"),
                        mcp_server=mcp_server_of(name),
                    )
                    seen_digests.add(digest)
                    index += 1
                    traj.tool_events.append(ev)
                    if ev.tool_use_id:
                        pending[ev.tool_use_id] = ev

                    # Time to first code modification: the first *successful* mutating call
                    # is confirmed later, when its tool_result arrives.
                    if ts and ev.tool_use_id and (
                        kind in _MUTATING_KINDS
                        or (
                            kind in ("bash", "git")
                            and isinstance(tool_input, dict)
                            and _MUTATING_BASH.search(str(tool_input.get("command") or ""))
                        )
                    ):
                        mutating_at[ev.tool_use_id] = ts

        elif etype == "user":
            message = event.get("message") or {}
            content = message.get("content")
            ts = _parse_ts(event.get("timestamp"))
            for block in content if isinstance(content, list) else []:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                ev = pending.pop(str(block.get("tool_use_id")), None)
                if ev is None:
                    continue
                ev.completed_at = event.get("timestamp")
                ev.is_error = bool(block.get("is_error"))
                if ev.is_error:
                    body = block.get("content")
                    ev.error_summary = (
                        body if isinstance(body, str) else json.dumps(body, default=str)
                    )[:500]
                start = _parse_ts(ev.started_at)
                if start and ts:
                    ev.duration_ms = max(0, int((ts - start).total_seconds() * 1000))
                edit_ts = mutating_at.pop(ev.tool_use_id or "", None)
                if edit_ts and not ev.is_error and traj.first_edit_at is None:
                    traj.first_edit_at = edit_ts

        elif etype == "result":
            traj.result_event = event
            if not traj.session_id:
                traj.session_id = event.get("session_id")

    return traj


def build_tool_metrics(traj: Trajectory, mcp_requirement: Any = None) -> ToolMetrics:
    """Aggregate a trajectory's tool events into reportable metrics."""
    m = ToolMetrics()
    read_paths: list[str] = []
    modified_paths: set[str] = set()
    mcp_servers: list[str] = []
    mcp_tools_used: set[str] = set()

    for ev in traj.tool_events:
        m.total_calls += 1
        m.sequence.append(ev.name if ev.kind == "mcp" else ev.kind.capitalize())

        match ev.kind:
            case "bash":
                m.bash_calls += 1
            case "git":
                m.git_calls += 1
                m.bash_calls += 1  # git calls run through Bash; count in both views
            case "read":
                m.read_calls += 1
                if ev.target_path:
                    read_paths.append(ev.target_path)
            case "write":
                m.write_calls += 1
                if ev.target_path and not ev.is_error:
                    modified_paths.add(ev.target_path)
            case "edit":
                m.edit_calls += 1
                if ev.target_path and not ev.is_error:
                    modified_paths.add(ev.target_path)
            case "search":
                m.search_calls += 1
            case "mcp":
                m.mcp_calls += 1
                mcp_tools_used.add(ev.name)
                if ev.mcp_server:
                    mcp_servers.append(ev.mcp_server)
            case "web":
                m.web_calls += 1
            case "subagent":
                m.subagent_calls += 1
            case _:
                m.other_calls += 1

        if ev.is_error:
            m.failed_calls += 1
        if ev.is_repeat:
            m.repeated_calls += 1
            if ev.kind == "mcp":
                m.mcp_redundant_calls += 1

    m.repeated_reads = len(read_paths) - len(set(read_paths))
    m.distinct_files_read = len(set(read_paths))
    m.distinct_files_modified = len(modified_paths)
    m.mcp_servers_used = sorted(set(mcp_servers))

    if mcp_requirement is not None:
        expected = set(getattr(mcp_requirement, "expected_tools", []) or [])
        forbidden = set(getattr(mcp_requirement, "forbidden_tools", []) or [])
        m.mcp_expected_tools_used = sorted(expected & mcp_tools_used)
        m.mcp_missing_tools = sorted(expected - mcp_tools_used)
        m.mcp_forbidden_tools_used = sorted(forbidden & mcp_tools_used)
        max_calls = getattr(mcp_requirement, "max_calls", None)
        if max_calls is not None and m.mcp_calls > max_calls:
            m.mcp_redundant_calls = max(m.mcp_redundant_calls, m.mcp_calls - max_calls)

    return m
