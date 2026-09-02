"""CLI surface: every documented command runs and behaves sensibly."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from acb.cli import app
from acb.config import Paths, Workspace

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def invoke(*args: str):
    return runner.invoke(app, list(args))


class TestConfigLoading:
    def test_workspace_loads_the_shipped_configuration(self):
        ws = Workspace.load(ROOT)
        assert ws.tasks and ws.targets and ws.suites
        assert ws.weights.functional_correctness == 0.50

    def test_paths_resolve_under_the_project_root(self):
        paths = Paths.resolve(ROOT)
        assert paths.targets_file == ROOT / "config" / "targets.yaml"
        assert paths.tasks_dir == ROOT / "tasks"

    def test_resolving_a_suite_returns_its_tasks_in_order(self):
        ws = Workspace.load(ROOT)
        tasks = ws.resolve_tasks(None, "smoke")
        assert [t.id for t in tasks] == ws.suites["smoke"].tasks

    def test_unknown_suite_is_rejected(self):
        with pytest.raises(KeyError, match="unknown suite"):
            Workspace.load(ROOT).resolve_tasks(None, "nope")

    def test_unknown_task_is_rejected(self):
        with pytest.raises(KeyError, match="unknown task"):
            Workspace.load(ROOT).resolve_tasks(["nope"], None)

    def test_unknown_target_is_rejected(self):
        with pytest.raises(KeyError, match="unknown target"):
            Workspace.load(ROOT).resolve_targets(["nope"])

    def test_disabled_targets_are_excluded_by_default(self):
        ws = Workspace.load(ROOT)
        selected = {t.id for t in ws.resolve_targets(None)}
        disabled = {t.id for t in ws.targets.values() if not t.enabled}
        assert disabled
        assert not (selected & disabled)

    def test_disabled_target_can_still_be_named_explicitly(self):
        ws = Workspace.load(ROOT)
        disabled = next(t.id for t in ws.targets.values() if not t.enabled)
        assert [t.id for t in ws.resolve_targets([disabled])] == [disabled]


class TestCommands:
    def test_version(self):
        result = invoke("--version")
        assert result.exit_code == 0
        assert "acb" in result.stdout

    def test_list_tasks(self):
        result = invoke("list", "tasks", "--root", str(ROOT))
        assert result.exit_code == 0
        assert "payments-bug-001" in result.stdout

    def test_list_tasks_filtered_by_category(self):
        result = invoke("list", "tasks", "--category", "mcp", "--root", str(ROOT))
        assert result.exit_code == 0
        assert "payments-mcp-001" in result.stdout
        assert "payments-bug-001" not in result.stdout

    def test_list_targets(self):
        result = invoke("list", "targets", "--root", str(ROOT))
        assert result.exit_code == 0
        assert "claude-opus-anthropic" in result.stdout
        assert "glm-fireworks" in result.stdout

    def test_list_suites(self):
        result = invoke("list", "suites", "--root", str(ROOT))
        assert result.exit_code == 0
        assert "smoke" in result.stdout

    def test_validate_passes_on_the_shipped_configuration(self):
        result = invoke("validate", "--root", str(ROOT))
        assert result.exit_code == 0, result.stdout
        assert "valid" in result.stdout

    def test_init_creates_directories_and_registers_config(self, tmp_path: Path):
        import shutil

        for name in ("tasks", "config", "fixtures"):
            shutil.copytree(ROOT / name, tmp_path / name)
        result = invoke("init", "--root", str(tmp_path))
        assert result.exit_code == 0, result.stdout
        assert (tmp_path / "acb.sqlite3").exists()
        assert (tmp_path / "runs").is_dir()
        assert "registered" in result.stdout

    def test_run_dry_run_shows_the_plan_without_executing(self):
        result = invoke(
            "run", "--suite", "smoke", "--target", "claude-opus-anthropic",
            "--runs", "2", "--dry-run", "--root", str(ROOT),
        )
        assert result.exit_code == 0
        assert "4 runs" in result.stdout

    def test_run_rejects_an_unknown_target(self):
        result = invoke("run", "--task", "payments-bug-001", "--target", "nope",
                        "--root", str(ROOT))
        assert result.exit_code == 2

    def test_doctor_reports_on_the_environment(self):
        result = invoke("doctor", "--root", str(ROOT))
        # Exit code depends on whether credentials are configured on this machine, so
        # assert on the checks performed rather than the verdict.
        assert "Environment" in result.stdout
        assert "Fixtures" in result.stdout
        assert "Targets" in result.stdout

    def test_compare_without_runs_exits_cleanly(self, tmp_path: Path):
        result = invoke("compare", "--root", str(tmp_path))
        assert result.exit_code == 1
        assert "No runs" in result.stdout

    def test_report_without_runs_exits_cleanly(self, tmp_path: Path):
        result = invoke("report", "--root", str(tmp_path))
        assert result.exit_code == 1

    def test_show_missing_run(self, tmp_path: Path):
        result = invoke("show", "nonexistent", "--root", str(tmp_path))
        assert result.exit_code == 1
        assert "not found" in result.stdout

    def test_prune_runs_cleanly(self):
        assert invoke("prune").exit_code == 0


class TestEndToEndWithMockAgent:
    """A full run -> compare -> report -> show cycle, with no model calls."""

    @pytest.fixture
    def project(self, tmp_path: Path) -> Path:
        import shutil

        for name in ("tasks", "config", "fixtures", "mcp_servers"):
            shutil.copytree(ROOT / name, tmp_path / name)
        # Point the MCP config at the copied server.
        mcp = tmp_path / "config" / "mcp" / "spec.json"
        mcp.write_text(json.dumps({"mcpServers": {"specs": {
            "command": "python3", "args": [str(tmp_path / "mcp_servers" / "spec_server.py")]
        }}}))
        invoke("init", "--root", str(tmp_path))
        return tmp_path

    def test_full_cycle(self, project: Path):
        result = invoke(
            "run", "--task", "payments-bug-001", "--target", "claude-opus-anthropic",
            "--runs", "2", "--mock", "--root", str(project),
        )
        assert result.exit_code == 0, result.stdout
        assert "mock agent" in result.stdout
        assert "2/2 successful" not in result.stdout  # the mock changes nothing, so it fails

        compare = invoke("compare", "--root", str(project))
        assert compare.exit_code == 0, compare.stdout
        assert "claude-opus-anthropic" in compare.stdout

        report = invoke("report", "--root", str(project))
        assert report.exit_code == 0, report.stdout
        assert (project / "reports" / "report.html").exists()
        assert (project / "reports" / "report.json").exists()
        assert (project / "reports" / "report.csv").exists()

    def test_runs_are_persisted_and_inspectable(self, project: Path):
        invoke("run", "--task", "payments-bug-001", "--target", "claude-opus-anthropic",
               "--mock", "--root", str(project))
        from acb.db.repository import Database

        db = Database(project / "acb.sqlite3")
        records = db.load_runs()
        assert len(records) == 1

        show = invoke("show", records[0].run_id, "--artifacts", "--root", str(project))
        assert show.exit_code == 0
        assert "payments-bug-001" in show.stdout
        assert "metadata.json" in show.stdout

    def test_export_writes_raw_records(self, project: Path, tmp_path: Path):
        invoke("run", "--task", "payments-bug-001", "--target", "claude-opus-anthropic",
               "--mock", "--root", str(project))
        out = tmp_path / "export.json"
        result = invoke("export", "--output", str(out), "--root", str(project))
        assert result.exit_code == 0
        payload = json.loads(out.read_text())
        assert payload[0]["task_id"] == "payments-bug-001"

    def test_reprice_current_is_labelled_as_a_what_if(self, project: Path):
        invoke("run", "--task", "payments-bug-001", "--target", "claude-opus-anthropic",
               "--mock", "--root", str(project))
        result = invoke("report", "--reprice-current", "--root", str(project))
        assert result.exit_code == 0
        assert "what-if" in result.stdout

    def test_break_even_command_runs(self, project: Path):
        invoke("run", "--task", "payments-bug-001", "--target", "claude-opus-anthropic",
               "--mock", "--root", str(project))
        result = invoke(
            "break-even", "--api-target", "glm-fireworks",
            "--self-hosted-target", "glm-self-hosted", "--root", str(project),
        )
        assert result.exit_code == 0
        assert "Insufficient data" in result.stdout or "ESTIMATE" in result.stdout


class TestLocalMcpServer:
    """The bundled MCP server must speak the protocol, or the MCP category is untestable."""

    def _rpc(self, requests: list[dict]) -> list[dict]:
        import subprocess

        payload = "\n".join(json.dumps(r) for r in requests) + "\n"
        proc = subprocess.run(
            ["python3", str(ROOT / "mcp_servers" / "spec_server.py")],
            input=payload, capture_output=True, text=True, timeout=30,
        )
        return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]

    def test_initialize_and_list_tools(self):
        responses = self._rpc([
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ])
        assert responses[0]["result"]["serverInfo"]["name"] == "acb-spec-server"
        names = {t["name"] for t in responses[1]["result"]["tools"]}
        assert {"get_jira_issue", "get_confluence_page"} <= names

    def test_jira_issue_links_to_the_spec_page(self):
        responses = self._rpc([{
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_jira_issue", "arguments": {"key": "PAY-142"}},
        }])
        issue = json.loads(responses[0]["result"]["content"][0]["text"])
        assert issue["linked_pages"] == ["CONF-REFUND-2"]

    def test_spec_page_contains_the_non_obvious_rules(self):
        """These rules exist ONLY here -- an agent must retrieve them to pass."""
        responses = self._rpc([{
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "get_confluence_page", "arguments": {"page_id": "CONF-REFUND-2"}},
        }])
        body = json.loads(responses[0]["result"]["content"][0]["text"])["body"]
        assert "fee is NOT returned" in body.replace("**", "")
        assert "RefundAmountTooLarge" in body

    def test_unknown_tool_returns_an_error_result(self):
        responses = self._rpc([{
            "jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "nope", "arguments": {}},
        }])
        assert responses[0]["result"]["isError"] is True

    def test_notifications_produce_no_response(self):
        responses = self._rpc([
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        ])
        assert len(responses) == 1
        assert responses[0]["id"] == 1

    def test_malformed_input_does_not_kill_the_server(self):
        import subprocess

        proc = subprocess.run(
            ["python3", str(ROOT / "mcp_servers" / "spec_server.py")],
            input='{bad json}\n{"jsonrpc":"2.0","id":9,"method":"ping"}\n',
            capture_output=True, text=True, timeout=30,
        )
        assert '"id": 9' in proc.stdout
