"""Shared fixtures. Nothing here touches a network or spends money."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from acb.models.pricing import (
    GpuSpec,
    InfrastructurePricing,
    SubscriptionPricing,
    TokenPricing,
    UnknownPricing,
)
from acb.models.target import TargetConfig
from acb.models.task import TaskSpec


@pytest.fixture
def token_pricing() -> TokenPricing:
    return TokenPricing(
        currency="USD",
        effective_date="2026-01-01",
        input_per_million_tokens=3.0,
        output_per_million_tokens=15.0,
        cached_input_per_million_tokens=0.30,
        cache_write_per_million_tokens=3.75,
    )


@pytest.fixture
def infra_pricing() -> InfrastructurePricing:
    return InfrastructurePricing(
        currency="USD",
        gpu=GpuSpec(type="B200", count=4, hourly_cost_per_gpu=4.50),
        additional_machine_hourly_cost=1.20,
        utilization_assumption=0.40,
    )


@pytest.fixture
def subscription_pricing() -> SubscriptionPricing:
    return SubscriptionPricing(
        monthly_cost=200.0, allocation_strategy="active_user", seats=1, tasks_per_month=100
    )


@pytest.fixture
def anthropic_target(token_pricing) -> TargetConfig:
    return TargetConfig(
        id="test-anthropic",
        model="opus",
        provider="anthropic",
        deployment_type="api",
        adapter="anthropic",
        api_key_env="TEST_ANTHROPIC_KEY",
        pricing=token_pricing,
    )


@pytest.fixture
def openai_target(token_pricing) -> TargetConfig:
    return TargetConfig(
        id="test-openai",
        model="glm-x",
        model_family="glm",
        provider="fireworks",
        deployment_type="api",
        adapter="openai_compat",
        endpoint="https://proxy.example/v1",
        api_key_env="TEST_FIREWORKS_KEY",
        pricing=token_pricing,
    )


@pytest.fixture
def selfhosted_target(infra_pricing) -> TargetConfig:
    return TargetConfig(
        id="test-selfhosted",
        model="glm-local",
        model_family="glm",
        provider="internal",
        deployment_type="self_hosted",
        adapter="openai_compat",
        endpoint="http://gpu:8000/v1",
        pricing=infra_pricing,
    )


@pytest.fixture
def unknown_target() -> TargetConfig:
    return TargetConfig(
        id="test-unknown", model="mystery", provider="tbd", pricing=UnknownPricing()
    )


@pytest.fixture
def git_fixture_repo(tmp_path: Path) -> Path:
    """A tiny git repo with two commits, used to exercise workspace isolation."""
    repo = tmp_path / "fixture-repo"
    repo.mkdir()

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        ).stdout

    git("init", "-q", "-b", "main")
    git("config", "user.email", "test@acb.local")
    git("config", "user.name", "ACB Test")
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    git("add", "-A")
    git("commit", "-q", "-m", "initial")
    (repo / "notes.md").write_text("second commit\n")
    git("add", "-A")
    git("commit", "-q", "-m", "second")
    return repo


@pytest.fixture
def sample_task(git_fixture_repo: Path, tmp_path: Path) -> TaskSpec:
    return TaskSpec.model_validate(
        {
            "id": "test-task-001",
            "name": "Fix add()",
            "category": "bug_fix",
            "difficulty": "easy",
            "repository": {"path": str(git_fixture_repo), "base_commit": "HEAD"},
            "task": {"prompt": "add() subtracts instead of adding. Fix it."},
            "timeout_minutes": 1,
            "validation": [
                {"name": "unit-tests", "kind": "test", "command": "$ACB_PYTHON -m pytest"}
            ],
            "success": {"require_tests_pass": True, "require_clean_build": False},
            "tool_call_budget": 10,
        }
    )


def stream_event(**kwargs) -> dict:
    return kwargs


@pytest.fixture
def trajectory_events() -> list[dict]:
    """A realistic stream-json trajectory, shaped exactly like observed CLI output."""
    return [
        {
            "type": "system",
            "subtype": "init",
            "session_id": "sess-1",
            "cwd": "/ws",
            "model": "claude-opus-5",
            "permissionMode": "bypassPermissions",
            "tools": ["Bash", "Read", "Edit"],
            "mcp_servers": [{"name": "specs", "status": "connected"}],
            "claude_code_version": "2.1.252",
        },
        {
            "type": "assistant",
            "timestamp": "2026-09-01T10:00:00.000Z",
            "message": {
                "model": "claude-opus-5",
                "content": [
                    {"type": "text", "text": "Looking for the bug."},
                    {"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "add"}},
                ],
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 5000,
                    "cache_read_input_tokens": 0,
                },
            },
        },
        {
            "type": "user",
            "timestamp": "2026-09-01T10:00:01.000Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t1", "content": "hit"}]},
        },
        {
            "type": "assistant",
            "timestamp": "2026-09-01T10:00:02.000Z",
            "message": {
                "model": "claude-opus-5",
                "content": [
                    {"type": "tool_use", "id": "t2", "name": "Read",
                     "input": {"file_path": "/ws/calc.py"}}
                ],
                "usage": {
                    "input_tokens": 50,
                    "output_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 5000,
                },
            },
        },
        {
            "type": "user",
            "timestamp": "2026-09-01T10:00:03.000Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t2", "content": "src"}]},
        },
        # A repeated read of the same file -- should be counted as a repeat.
        {
            "type": "assistant",
            "timestamp": "2026-09-01T10:00:04.000Z",
            "message": {
                "model": "claude-opus-5",
                "content": [
                    {"type": "tool_use", "id": "t3", "name": "Read",
                     "input": {"file_path": "/ws/calc.py"}}
                ],
                "usage": {"input_tokens": 30, "output_tokens": 5},
            },
        },
        {
            "type": "user",
            "timestamp": "2026-09-01T10:00:05.000Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t3", "content": "src"}]},
        },
        # A failing bash call.
        {
            "type": "assistant",
            "timestamp": "2026-09-01T10:00:06.000Z",
            "message": {
                "model": "claude-opus-5",
                "content": [
                    {"type": "tool_use", "id": "t4", "name": "Bash",
                     "input": {"command": "pytset -q"}}
                ],
                "usage": {"input_tokens": 20, "output_tokens": 5},
            },
        },
        {
            "type": "user",
            "timestamp": "2026-09-01T10:00:07.000Z",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t4",
                     "content": "command not found", "is_error": True}
                ]
            },
        },
        # A successful edit -- this timestamp is time-to-first-edit.
        {
            "type": "assistant",
            "timestamp": "2026-09-01T10:00:08.000Z",
            "message": {
                "model": "claude-opus-5",
                "content": [
                    {"type": "tool_use", "id": "t5", "name": "Edit",
                     "input": {"file_path": "/ws/calc.py", "old_string": "-", "new_string": "+"}}
                ],
                "usage": {"input_tokens": 40, "output_tokens": 30},
            },
        },
        {
            "type": "user",
            "timestamp": "2026-09-01T10:00:09.000Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t5", "content": "ok"}]},
        },
        # An MCP call.
        {
            "type": "assistant",
            "timestamp": "2026-09-01T10:00:10.000Z",
            "message": {
                "model": "claude-opus-5",
                "content": [
                    {"type": "tool_use", "id": "t6", "name": "mcp__specs__get_jira_issue",
                     "input": {"key": "PAY-142"}}
                ],
                "usage": {"input_tokens": 25, "output_tokens": 8},
            },
        },
        {
            "type": "user",
            "timestamp": "2026-09-01T10:00:11.000Z",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "t6", "content": "{}"}]},
        },
        {
            "type": "result",
            "subtype": "success",
            "session_id": "sess-1",
            "is_error": False,
            "num_turns": 6,
            "duration_ms": 12000,
            "duration_api_ms": 9000,
            "ttft_ms": 800,
            "total_cost_usd": 0.42,
            "terminal_reason": "completed",
            "permission_denials": [],
            "usage": {"input_tokens": 265, "output_tokens": 78},
            "modelUsage": {
                "claude-opus-5": {"costUSD": 0.42, "provider": "firstParty", "costBasis": "list"}
            },
        },
    ]


@pytest.fixture
def trajectory_lines(trajectory_events) -> list[str]:
    return [json.dumps(e) for e in trajectory_events]
