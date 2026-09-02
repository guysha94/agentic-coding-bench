# Adding a provider or deployment

Most providers need **no code** — only a target entry naming an existing adapter. Write a
new adapter only when the provider speaks a genuinely new *usage dialect*.

---

## No code needed

| Situation | What to do |
|---|---|
| New API provider speaking the Anthropic Messages API | Target with `adapter: anthropic` and an `endpoint` |
| New API provider behind an OpenAI→Anthropic shim | Target with `adapter: openai_compat` and the shim's `endpoint` |
| Same model on new hardware | New target, `deployment_type: self_hosted`, infrastructure pricing |
| New pricing arrangement for an existing provider | New target with a different `pricing:` block |

## Self-hosted deployment

```yaml
  glm-self-hosted-h100:
    model: my-org/glm-local
    model_family: glm
    provider: internal
    deployment_type: self_hosted
    adapter: openai_compat
    endpoint: http://gpu-server:8000/v1
    api_key_env: SELF_HOSTED_API_KEY
    pricing:
      type: infrastructure
      currency: USD
      gpu:
        type: H100
        count: 8
        hourly_cost_per_gpu: 2.50
      additional_machine_hourly_cost: 1.00
      utilization_assumption: 0.35
```

`deployment_type: self_hosted` requires an `endpoint`; the config validator enforces it.

---

## Writing a new adapter

Needed only when a provider reports usage in a way neither dialect covers.

```python
# src/acb/adapters/my_provider.py
from acb.adapters.base import ProviderAdapter
from acb.models.usage import NormalizedUsage


class MyProviderAdapter(ProviderAdapter):
    name = "my_provider"          # the value targets put in `adapter:`
    dialect = "my_provider_v1"    # recorded per run, so usage can be re-normalised later

    def agent_env(self, operator_env: dict[str, str]) -> dict[str, str]:
        """Only the variables that route Claude Code to this provider."""
        env = {}
        if self.target.endpoint:
            env["ANTHROPIC_BASE_URL"] = self.target.endpoint
        if self.target.api_key_env:
            secret = operator_env.get(self.target.api_key_env)
            if secret:
                env["ANTHROPIC_AUTH_TOKEN"] = secret
        env.update(self.target.resolve_env(operator_env))
        return env

    def normalize_usage(self, payloads: list[dict]) -> NormalizedUsage:
        usage = self._base_usage()
        for p in payloads:
            # THE critical question: does the prompt count already include cached tokens?
            usage.input_tokens += self._int(p, "billable_prompt_tokens")
            usage.cached_input_tokens += self._int(p, "cached_prompt_tokens")
            usage.output_tokens += self._int(p, "completion_tokens")
            usage.total_turns += 1
        return usage
```

Register it:

```python
# src/acb/adapters/registry.py
from acb.adapters.my_provider import MyProviderAdapter

_ADAPTERS = {..., MyProviderAdapter.name: MyProviderAdapter}
```

### The one thing to get right

Answer this before writing `normalize_usage`:

> **Does the provider's prompt-token count already include cached tokens?**

- **Yes** (OpenAI convention): subtract them — `input = prompt − cached`.
- **No** (Anthropic convention): pass `input` through unchanged.

Getting this backwards produces cost numbers that look reasonable and are wrong by the
cache ratio, which on agentic workloads is often the majority of all tokens.

Also handle absence honestly: if the provider does not report cache fields at all, set
`usage.complete = False` and add a note. That marks the cost `incomplete` instead of
silently reporting cached tokens as zero.

### Tests to add

Mirror `tests/test_usage_normalization.py`:

- a payload with cached tokens normalises to the right billable input
- the same underlying request yields the same `total_billable_input` as the other dialects
- a missing cache field sets `complete = False`
- inconsistent values are clamped rather than producing a negative charge
- `agent_env` injects the credential and omits it when absent

---

## Provider-specific token classes

For billable counters that are not tokens:

```yaml
    pricing:
      type: token
      input_per_million_tokens: 1.0
      output_per_million_tokens: 3.0
      extra_token_classes:
        web_search_requests: 0.01     # per unit
```

Populate `usage.extra["web_search_requests"]` in your adapter; the cost engine multiplies
the two.
