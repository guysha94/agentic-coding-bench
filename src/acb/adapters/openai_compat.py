"""OpenAI-compatible dialect, reached through a translating proxy.

Claude Code speaks the Anthropic Messages API only, so an OpenAI-shaped provider
(Fireworks, vLLM, SGLang, Together, ...) requires a proxy in front of it. That proxy is
part of the system under test -- see docs/claude-code-model-replacement.md Sec 3.

Convention -- the opposite of Anthropic, and the reason this adapter exists:

    prompt_tokens                          INCLUDES prompt_tokens_details.cached_tokens
    prompt_tokens_details.cached_tokens    cache READ (already inside prompt_tokens)
    completion_tokens                      INCLUDES reasoning_tokens

So billable input must be computed as `prompt_tokens - cached_tokens`. Failing to do this
double-counts cached tokens and overstates cost, which is exactly the mistake the brief
warns about.
"""

from __future__ import annotations

from typing import Any

from acb.adapters.base import ProviderAdapter
from acb.models.usage import NormalizedUsage


class OpenAICompatAdapter(ProviderAdapter):
    name = "openai_compat"
    dialect = "openai_compat_v1"

    def agent_env(self, operator_env: dict[str, str]) -> dict[str, str]:
        env: dict[str, str] = {}
        t = self.target
        if t.endpoint:
            # The endpoint must be an Anthropic-Messages-compatible shim in front of the
            # OpenAI-compatible provider.
            env["ANTHROPIC_BASE_URL"] = t.endpoint
        if t.api_key_env:
            secret = operator_env.get(t.api_key_env)
            if secret:
                env["ANTHROPIC_AUTH_TOKEN"] = secret
        env.update(t.resolve_env(operator_env))
        return env

    def normalize_usage(self, payloads: list[dict[str, Any]]) -> NormalizedUsage:
        usage = self._base_usage()
        saw_cache_field = False
        clamped = False

        for p in payloads:
            if not p:
                continue

            prompt = self._int(p, "prompt_tokens", "input_tokens", "inputTokens")
            completion = self._int(p, "completion_tokens", "output_tokens", "outputTokens")

            details = p.get("prompt_tokens_details") or {}
            cached = 0
            if isinstance(details, dict) and "cached_tokens" in details:
                cached = self._int(details, "cached_tokens")
                saw_cache_field = True
            elif "cache_read_input_tokens" in p:
                # Some shims emit the Anthropic field name while keeping OpenAI semantics.
                cached = self._int(p, "cache_read_input_tokens")
                saw_cache_field = True

            billable = prompt - cached
            if billable < 0:
                # Inconsistent payload: never let it produce a negative charge.
                clamped = True
                billable = 0

            usage.input_tokens += billable
            usage.cached_input_tokens += cached
            usage.output_tokens += completion

            # Most OpenAI-compatible providers have no explicit cache-write class; when a
            # shim does expose one, honour it.
            usage.cache_write_tokens += self._int(
                p, "cache_creation_input_tokens", "cache_write_tokens"
            )

            comp_details = p.get("completion_tokens_details") or {}
            if isinstance(comp_details, dict):
                usage.reasoning_tokens += self._int(comp_details, "reasoning_tokens")
            elif isinstance(p.get("output_tokens_details"), dict):
                usage.reasoning_tokens += self._int(p["output_tokens_details"], "thinking_tokens")

            usage.total_turns += 1

        if payloads and not saw_cache_field:
            usage.complete = False
            usage.notes.append(
                "proxy reported no cached-token field; prompt caching cannot be verified "
                "and cache cost is treated as unknown"
            )
        if clamped:
            usage.complete = False
            usage.notes.append(
                "cached_tokens exceeded prompt_tokens in at least one payload; billable "
                "input clamped to 0 (suspect proxy usage translation)"
            )
        return usage
