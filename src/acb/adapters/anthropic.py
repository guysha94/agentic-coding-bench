"""Anthropic Messages API dialect (first-party, Bedrock, Vertex, Foundry, and any
Anthropic-compatible gateway).

Convention -- verified against live `claude -p --output-format stream-json` output:

    input_tokens                    EXCLUDES cache reads and cache creation
    cache_read_input_tokens         cache READ  (separate, discounted)
    cache_creation_input_tokens     cache WRITE (separate, premium)
    output_tokens                   INCLUDES output_tokens_details.thinking_tokens

So `input_tokens` passes through unmodified: there is nothing to subtract. Reasoning
tokens are recorded but treated as a subset of output (the cost engine subtracts them only
when a separate reasoning rate is configured).
"""

from __future__ import annotations

from typing import Any

from acb.adapters.base import ProviderAdapter
from acb.models.usage import NormalizedUsage


class AnthropicAdapter(ProviderAdapter):
    name = "anthropic"
    dialect = "anthropic_v1"

    def agent_env(self, operator_env: dict[str, str]) -> dict[str, str]:
        env: dict[str, str] = {}
        t = self.target

        if t.endpoint:
            # Points Claude Code at a non-first-party Anthropic-compatible endpoint.
            env["ANTHROPIC_BASE_URL"] = t.endpoint

        if t.api_key_env:
            secret = operator_env.get(t.api_key_env)
            if secret:
                # A first-party key uses ANTHROPIC_API_KEY; a gateway/proxy credential is
                # passed as ANTHROPIC_AUTH_TOKEN (sent as a bearer token).
                var = "ANTHROPIC_API_KEY" if not t.endpoint else "ANTHROPIC_AUTH_TOKEN"
                env[var] = secret

        env.update(t.resolve_env(operator_env))
        return env

    def normalize_usage(self, payloads: list[dict[str, Any]]) -> NormalizedUsage:
        usage = self._base_usage()
        saw_cache_field = False

        for p in payloads:
            if not p:
                continue
            usage.input_tokens += self._int(p, "input_tokens", "inputTokens")
            usage.output_tokens += self._int(p, "output_tokens", "outputTokens")

            cache_read = self._int(p, "cache_read_input_tokens", "cacheReadInputTokens")
            cache_write = self._int(p, "cache_creation_input_tokens", "cacheCreationInputTokens")
            if any(
                k in p
                for k in (
                    "cache_read_input_tokens",
                    "cacheReadInputTokens",
                    "cache_creation_input_tokens",
                    "cacheCreationInputTokens",
                )
            ):
                saw_cache_field = True
            usage.cached_input_tokens += cache_read
            usage.cache_write_tokens += cache_write

            details = p.get("output_tokens_details") or {}
            if isinstance(details, dict):
                usage.reasoning_tokens += self._int(details, "thinking_tokens", "reasoning_tokens")

            server_tools = p.get("server_tool_use") or {}
            if isinstance(server_tools, dict):
                for key, val in server_tools.items():
                    try:
                        n = float(val)
                    except (TypeError, ValueError):
                        continue
                    if n:
                        usage.extra[key] = usage.extra.get(key, 0.0) + n

            usage.total_turns += 1

        if payloads and not saw_cache_field:
            # A proxy that drops cache fields makes cost unknowable, not zero.
            usage.complete = False
            usage.notes.append(
                "provider reported no prompt-cache fields; cache cost cannot be verified"
            )
        return usage
