"""Usage normalisation -- the double-counting rules.

These are the highest-value tests in the suite: an error here silently corrupts every
cost number in every report.
"""

from __future__ import annotations

import pytest

from acb.adapters.anthropic import AnthropicAdapter
from acb.adapters.openai_compat import OpenAICompatAdapter
from acb.adapters.registry import available_adapters, get_adapter
from acb.models.usage import NORMALIZATION_VERSION, NormalizedUsage


class TestAnthropicDialect:
    """Anthropic EXCLUDES cache tokens from input_tokens, so input passes through."""

    def test_input_tokens_pass_through_unmodified(self, anthropic_target):
        usage = AnthropicAdapter(anthropic_target).normalize_usage(
            [
                {
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 9000,
                    "cache_creation_input_tokens": 1000,
                }
            ]
        )
        assert usage.input_tokens == 100, "cache tokens must NOT be subtracted from input"
        assert usage.cached_input_tokens == 9000
        assert usage.cache_write_tokens == 1000
        assert usage.output_tokens == 50

    def test_total_billable_input_sums_all_input_classes(self, anthropic_target):
        usage = AnthropicAdapter(anthropic_target).normalize_usage(
            [{"input_tokens": 100, "cache_read_input_tokens": 200,
              "cache_creation_input_tokens": 300}]
        )
        assert usage.total_billable_input == 600

    def test_thinking_tokens_are_recorded_as_a_subset_of_output(self, anthropic_target):
        usage = AnthropicAdapter(anthropic_target).normalize_usage(
            [{"input_tokens": 10, "output_tokens": 500,
              "output_tokens_details": {"thinking_tokens": 400}}]
        )
        assert usage.output_tokens == 500
        assert usage.reasoning_tokens == 400
        assert usage.reasoning_tokens <= usage.output_tokens

    def test_multiple_payloads_are_summed_and_turns_counted(self, anthropic_target):
        usage = AnthropicAdapter(anthropic_target).normalize_usage(
            [{"input_tokens": 10, "output_tokens": 5}, {"input_tokens": 20, "output_tokens": 7}]
        )
        assert (usage.input_tokens, usage.output_tokens, usage.total_turns) == (30, 12, 2)

    def test_missing_cache_fields_marks_usage_incomplete(self, anthropic_target):
        """A proxy that drops cache reporting makes cost unknowable, not zero."""
        usage = AnthropicAdapter(anthropic_target).normalize_usage(
            [{"input_tokens": 10, "output_tokens": 5}]
        )
        assert usage.complete is False
        assert any("cache" in n for n in usage.notes)

    def test_server_tool_use_is_captured_as_extra(self, anthropic_target):
        usage = AnthropicAdapter(anthropic_target).normalize_usage(
            [{"input_tokens": 1, "cache_read_input_tokens": 0,
              "server_tool_use": {"web_search_requests": 3}}]
        )
        assert usage.extra["web_search_requests"] == 3

    def test_empty_payloads_produce_zero_usage(self, anthropic_target):
        usage = AnthropicAdapter(anthropic_target).normalize_usage([])
        assert usage.total_tokens == 0
        assert usage.complete is True

    def test_camel_case_keys_are_accepted(self, anthropic_target):
        """`result.modelUsage` uses camelCase; the same adapter must read both."""
        usage = AnthropicAdapter(anthropic_target).normalize_usage(
            [{"inputTokens": 11, "outputTokens": 4, "cacheReadInputTokens": 7,
              "cacheCreationInputTokens": 3}]
        )
        assert (usage.input_tokens, usage.cached_input_tokens, usage.cache_write_tokens) == (
            11, 7, 3
        )


class TestOpenAICompatDialect:
    """OpenAI INCLUDES cached tokens in prompt_tokens, so they must be subtracted."""

    def test_cached_tokens_are_subtracted_from_prompt_tokens(self, openai_target):
        usage = OpenAICompatAdapter(openai_target).normalize_usage(
            [
                {
                    "prompt_tokens": 10000,
                    "completion_tokens": 200,
                    "prompt_tokens_details": {"cached_tokens": 9000},
                }
            ]
        )
        assert usage.input_tokens == 1000, "billable input must exclude cached tokens"
        assert usage.cached_input_tokens == 9000
        # Total billable input still accounts for every prompt token exactly once.
        assert usage.total_billable_input == 10000

    def test_no_double_counting_across_dialects(self, anthropic_target, openai_target):
        """The same underlying request must yield the same billable totals either way."""
        anthropic = AnthropicAdapter(anthropic_target).normalize_usage(
            [{"input_tokens": 1000, "output_tokens": 200, "cache_read_input_tokens": 9000,
              "cache_creation_input_tokens": 0}]
        )
        openai = OpenAICompatAdapter(openai_target).normalize_usage(
            [{"prompt_tokens": 10000, "completion_tokens": 200,
              "prompt_tokens_details": {"cached_tokens": 9000}}]
        )
        assert anthropic.input_tokens == openai.input_tokens == 1000
        assert anthropic.cached_input_tokens == openai.cached_input_tokens == 9000
        assert anthropic.total_billable_input == openai.total_billable_input == 10000

    def test_cached_exceeding_prompt_is_clamped_and_flagged(self, openai_target):
        usage = OpenAICompatAdapter(openai_target).normalize_usage(
            [{"prompt_tokens": 100, "completion_tokens": 5,
              "prompt_tokens_details": {"cached_tokens": 500}}]
        )
        assert usage.input_tokens == 0, "must never produce a negative charge"
        assert usage.complete is False
        assert any("clamped" in n for n in usage.notes)

    def test_reasoning_tokens_from_completion_details(self, openai_target):
        usage = OpenAICompatAdapter(openai_target).normalize_usage(
            [{"prompt_tokens": 10, "completion_tokens": 300,
              "prompt_tokens_details": {"cached_tokens": 0},
              "completion_tokens_details": {"reasoning_tokens": 250}}]
        )
        assert usage.reasoning_tokens == 250

    def test_missing_cache_field_marks_incomplete(self, openai_target):
        usage = OpenAICompatAdapter(openai_target).normalize_usage(
            [{"prompt_tokens": 100, "completion_tokens": 5}]
        )
        assert usage.complete is False

    def test_anthropic_style_cache_key_is_honoured(self, openai_target):
        """Some shims emit Anthropic field names while keeping OpenAI semantics."""
        usage = OpenAICompatAdapter(openai_target).normalize_usage(
            [{"prompt_tokens": 1000, "completion_tokens": 10, "cache_read_input_tokens": 600}]
        )
        assert usage.input_tokens == 400
        assert usage.cached_input_tokens == 600


class TestUsageModel:
    def test_addition_combines_all_fields(self):
        a = NormalizedUsage(input_tokens=1, output_tokens=2, cached_input_tokens=3,
                            cache_write_tokens=4, reasoning_tokens=5, total_turns=1,
                            extra={"x": 1.0})
        b = NormalizedUsage(input_tokens=10, output_tokens=20, cached_input_tokens=30,
                            cache_write_tokens=40, reasoning_tokens=50, total_turns=2,
                            extra={"x": 2.0, "y": 1.0})
        total = a + b
        assert (total.input_tokens, total.output_tokens, total.total_turns) == (11, 22, 3)
        assert total.extra == {"x": 3.0, "y": 1.0}

    def test_incompleteness_propagates_through_addition(self):
        good = NormalizedUsage(input_tokens=1)
        bad = NormalizedUsage(input_tokens=1, complete=False, notes=["missing cache"])
        assert (good + bad).complete is False
        assert "missing cache" in (good + bad).notes

    def test_mixed_dialects_are_labelled(self):
        a = NormalizedUsage(dialect="anthropic_v1")
        b = NormalizedUsage(dialect="openai_compat_v1")
        assert (a + b).dialect == "mixed"

    def test_normalization_version_is_recorded(self, anthropic_target):
        usage = AnthropicAdapter(anthropic_target).normalize_usage([{"input_tokens": 1}])
        assert usage.normalization_version == NORMALIZATION_VERSION
        assert usage.dialect == "anthropic_v1"


class TestAdapterRegistry:
    def test_known_adapters_resolve(self, anthropic_target, openai_target):
        assert isinstance(get_adapter(anthropic_target), AnthropicAdapter)
        assert isinstance(get_adapter(openai_target), OpenAICompatAdapter)

    def test_unknown_adapter_raises_with_a_helpful_message(self, anthropic_target):
        anthropic_target.adapter = "nope"
        with pytest.raises(ValueError, match="unknown adapter"):
            get_adapter(anthropic_target)

    def test_registry_lists_adapters(self):
        assert {"anthropic", "openai_compat"} <= set(available_adapters())


class TestAdapterEnvironment:
    def test_first_party_key_uses_api_key_variable(self, anthropic_target):
        env = AnthropicAdapter(anthropic_target).agent_env({"TEST_ANTHROPIC_KEY": "secret"})
        assert env["ANTHROPIC_API_KEY"] == "secret"
        assert "ANTHROPIC_BASE_URL" not in env

    def test_gateway_credential_uses_auth_token_and_base_url(self, anthropic_target):
        anthropic_target.endpoint = "https://gateway.example/v1"
        env = AnthropicAdapter(anthropic_target).agent_env({"TEST_ANTHROPIC_KEY": "secret"})
        assert env["ANTHROPIC_AUTH_TOKEN"] == "secret"
        assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example/v1"

    def test_openai_compat_points_at_the_shim(self, openai_target):
        env = OpenAICompatAdapter(openai_target).agent_env({"TEST_FIREWORKS_KEY": "k"})
        assert env["ANTHROPIC_BASE_URL"] == "https://proxy.example/v1"
        assert env["ANTHROPIC_AUTH_TOKEN"] == "k"

    def test_missing_credential_yields_no_variable(self, anthropic_target):
        env = AnthropicAdapter(anthropic_target).agent_env({})
        assert "ANTHROPIC_API_KEY" not in env

    def test_env_indirection_is_resolved(self, anthropic_target):
        anthropic_target.env = {"HTTPS_PROXY": "env:MY_PROXY", "LITERAL": "value"}
        env = AnthropicAdapter(anthropic_target).agent_env({"MY_PROXY": "http://p:3128"})
        assert env["HTTPS_PROXY"] == "http://p:3128"
        assert env["LITERAL"] == "value"

    def test_effort_becomes_a_cli_flag(self, anthropic_target):
        anthropic_target.model_params = {"effort": "xhigh"}
        assert AnthropicAdapter(anthropic_target).cli_args() == ["--effort", "xhigh"]
