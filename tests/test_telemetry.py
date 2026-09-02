"""Parsing the stream-json trajectory into tool events and usage."""

from __future__ import annotations

import json

from acb.models.task import McpRequirement
from acb.runner.telemetry import build_tool_metrics, classify_tool, mcp_server_of, parse_stream


class TestClassification:
    def test_known_tools_map_to_families(self):
        assert classify_tool("Bash") == "bash"
        assert classify_tool("Read") == "read"
        assert classify_tool("Edit") == "edit"
        assert classify_tool("MultiEdit") == "edit"
        assert classify_tool("Write") == "write"
        assert classify_tool("Grep") == "search"
        assert classify_tool("Glob") == "search"
        assert classify_tool("WebFetch") == "web"
        assert classify_tool("Task") == "subagent"

    def test_mcp_tools_are_detected_by_prefix(self):
        assert classify_tool("mcp__specs__get_jira_issue") == "mcp"
        assert mcp_server_of("mcp__specs__get_jira_issue") == "specs"
        assert mcp_server_of("Bash") is None

    def test_unknown_tools_fall_back_to_other(self):
        assert classify_tool("SomeFutureTool") == "other"


class TestParsing:
    def test_extracts_the_full_tool_sequence(self, trajectory_lines):
        traj = parse_stream(trajectory_lines)
        assert [e.name for e in traj.tool_events] == [
            "Grep", "Read", "Read", "Bash", "Edit", "mcp__specs__get_jira_issue"
        ]

    def test_captures_init_metadata(self, trajectory_lines):
        traj = parse_stream(trajectory_lines)
        assert traj.session_id == "sess-1"
        assert traj.model_reported == "claude-opus-5"
        assert traj.init_event["claude_code_version"] == "2.1.252"
        assert traj.mcp_servers() == [{"name": "specs", "status": "connected"}]
        assert traj.tools_available() == ["Bash", "Read", "Edit"]

    def test_captures_result_metadata(self, trajectory_lines):
        traj = parse_stream(trajectory_lines)
        assert traj.num_turns == 6
        assert traj.wall_clock_ms == 12000
        assert traj.api_ms == 9000
        assert traj.ttft_ms == 800
        assert traj.terminal_reason == "completed"
        assert traj.is_error is False
        assert traj.reported_cost_usd == 0.42

    def test_marks_failed_tool_results(self, trajectory_lines):
        traj = parse_stream(trajectory_lines)
        failed = [e for e in traj.tool_events if e.is_error]
        assert len(failed) == 1
        assert failed[0].name == "Bash"
        assert "command not found" in failed[0].error_summary

    def test_detects_repeated_calls(self, trajectory_lines):
        traj = parse_stream(trajectory_lines)
        repeats = [e for e in traj.tool_events if e.is_repeat]
        assert len(repeats) == 1
        assert repeats[0].target_path == "/ws/calc.py"

    def test_computes_per_call_duration(self, trajectory_lines):
        traj = parse_stream(trajectory_lines)
        assert traj.tool_events[0].duration_ms == 1000

    def test_time_to_first_edit_uses_the_first_successful_mutation(self, trajectory_lines):
        traj = parse_stream(trajectory_lines)
        # Session starts at 10:00:00; the successful Edit is issued at 10:00:08.
        assert traj.time_to_first_edit_ms() == 8000

    def test_failed_mutation_does_not_count_as_first_edit(self):
        lines = [
            json.dumps({
                "type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": [
                    {"type": "tool_use", "id": "a", "name": "Edit",
                     "input": {"file_path": "/x"}}], "usage": {}},
            }),
            json.dumps({
                "type": "user", "timestamp": "2026-01-01T00:00:01Z",
                "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "a", "is_error": True,
                     "content": "denied"}]},
            }),
        ]
        assert parse_stream(lines).time_to_first_edit_ms() is None

    def test_mutating_bash_counts_as_a_code_modification(self):
        lines = [
            json.dumps({
                "type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": [
                    {"type": "tool_use", "id": "a", "name": "Bash",
                     "input": {"command": "sed -i 's/a/b/' file.py"}}], "usage": {}},
            }),
            json.dumps({
                "type": "user", "timestamp": "2026-01-01T00:00:03Z",
                "message": {"content": [{"type": "tool_result", "tool_use_id": "a",
                                         "content": "ok"}]},
            }),
        ]
        assert parse_stream(lines).time_to_first_edit_ms() == 0

    def test_git_bash_commands_classified_as_git(self):
        lines = [json.dumps({
            "type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
            "message": {"content": [
                {"type": "tool_use", "id": "a", "name": "Bash",
                 "input": {"command": "git status --porcelain"}}], "usage": {}},
        })]
        assert parse_stream(lines).tool_events[0].kind == "git"

    def test_collects_usage_payloads_per_message(self, trajectory_lines):
        traj = parse_stream(trajectory_lines)
        assert len(traj.usage_payloads) == 6
        assert traj.usage_payloads[0]["cache_creation_input_tokens"] == 5000

    def test_malformed_lines_are_recorded_not_fatal(self):
        traj = parse_stream(["{not json}", '{"type":"result","num_turns":1}', ""])
        assert len(traj.parse_errors) == 1
        assert traj.result_event is not None

    def test_non_object_events_are_rejected(self):
        traj = parse_stream(["[1,2,3]"])
        assert traj.parse_errors and traj.events == []

    def test_empty_stream_yields_an_empty_trajectory(self):
        traj = parse_stream([])
        assert traj.tool_events == []
        assert traj.num_turns == 0
        assert traj.wall_clock_ms is None

    def test_accepts_a_raw_string(self, trajectory_lines):
        assert len(parse_stream("\n".join(trajectory_lines)).tool_events) == 6

    def test_permission_denials_counted(self):
        traj = parse_stream([json.dumps({
            "type": "result", "permission_denials": [{"tool": "Read"}, {"tool": "Bash"}]
        })])
        assert traj.permission_denials == 2


class TestToolMetrics:
    def test_counts_by_family(self, trajectory_lines):
        m = build_tool_metrics(parse_stream(trajectory_lines))
        assert m.total_calls == 6
        assert m.search_calls == 1
        assert m.read_calls == 2
        assert m.edit_calls == 1
        assert m.bash_calls == 1
        assert m.mcp_calls == 1

    def test_counts_failures_and_repeats(self, trajectory_lines):
        m = build_tool_metrics(parse_stream(trajectory_lines))
        assert m.failed_calls == 1
        assert m.repeated_calls == 1
        assert m.repeated_reads == 1
        assert m.distinct_files_read == 1
        assert m.distinct_files_modified == 1
        assert m.failed_call_rate == 1 / 6

    def test_records_the_ordered_sequence(self, trajectory_lines):
        m = build_tool_metrics(parse_stream(trajectory_lines))
        assert m.sequence[:5] == ["Search", "Read", "Read", "Bash", "Edit"]
        assert "Search -> Read" in m.sequence_string()

    def test_sequence_string_truncates_long_runs(self):
        from acb.models.run import ToolMetrics

        m = ToolMetrics(sequence=["Read"] * 100)
        assert "(+40)" in m.sequence_string(limit=60)

    def test_mcp_expectations_are_evaluated(self, trajectory_lines):
        requirement = McpRequirement(
            expected_tools=["mcp__specs__get_jira_issue", "mcp__specs__get_confluence_page"],
            forbidden_tools=["mcp__specs__list_confluence_pages"],
            max_calls=5,
        )
        m = build_tool_metrics(parse_stream(trajectory_lines), requirement)
        assert m.mcp_expected_tools_used == ["mcp__specs__get_jira_issue"]
        assert m.mcp_missing_tools == ["mcp__specs__get_confluence_page"]
        assert m.mcp_forbidden_tools_used == []
        assert m.mcp_servers_used == ["specs"]

    def test_exceeding_max_mcp_calls_counts_as_redundant(self):
        lines = []
        for i in range(10):
            lines.append(json.dumps({
                "type": "assistant", "timestamp": "2026-01-01T00:00:00Z",
                "message": {"content": [
                    {"type": "tool_use", "id": f"t{i}", "name": "mcp__specs__get_jira_issue",
                     "input": {"key": f"PAY-{i}"}}], "usage": {}},
            }))
        m = build_tool_metrics(parse_stream(lines), McpRequirement(max_calls=3))
        assert m.mcp_calls == 10
        assert m.mcp_redundant_calls == 7
