# Adding a model

Adding a model is a YAML change. No code, and no benchmark task edits.

## 1. Add a target

Append to `config/targets.yaml`:

```yaml
targets:
  my-model-provider:
    model: provider/model-identifier-v3   # exactly what the provider expects
    model_family: my-model                # analysis-level grouping
    display_name: My Model / Provider     # optional; defaults to "family / provider"
    provider: provider
    deployment_type: api                  # api | self_hosted | subscription | local
    adapter: openai_compat                # anthropic | openai_compat
    endpoint: https://shim.internal/v1
    api_key_env: PROVIDER_API_KEY         # name of the env var on YOUR machine
    model_params:
      effort: high                        # recorded for reproducibility; applied where supported
    pricing:
      type: token
      currency: USD
      effective_date: "2026-01-15"
      input_per_million_tokens: 0.50
      output_per_million_tokens: 1.50
      cached_input_per_million_tokens: null
      cache_write_per_million_tokens: null
```

**Model identifiers are free-form strings and are never validated against a list.**
Provider model ids change often — never hardcode an assumption that `glm` means a
particular version.

## 2. Verify

```bash
benchmark validate
benchmark doctor --targets my-model-provider
```

`doctor` checks that the adapter resolves, the credential env var is set, and the pricing
is complete.

## 3. Smoke test

```bash
benchmark run --suite smoke --target my-model-provider --runs 1
```

Then inspect what actually happened:

```bash
benchmark show <run-id> --artifacts
```

Check three things before trusting a full suite:

1. **The model reported back is the one you asked for.** `environment.model_reported` comes
   from the CLI, and a misconfigured proxy will silently serve something else.
2. **Token usage is populated**, including cache fields. If `usage.complete` is `false`,
   your cost numbers are a lower bound — see `docs/claude-code-model-replacement.md` §3.1.
3. **Tool calls happened at all.** Zero tool calls usually means tool schemas are not
   surviving the transport, not that the model is lazy.

## 4. Run the comparison

```bash
benchmark run --suite backend --targets claude-opus-anthropic,my-model-provider --runs 3
benchmark compare --targets claude-opus-anthropic,my-model-provider
```

## Choosing an adapter

| Your provider speaks | Adapter |
|---|---|
| The Anthropic Messages API (first-party, Bedrock, Vertex, a compatible gateway) | `anthropic` |
| An OpenAI-shaped API behind a translating shim | `openai_compat` |

The adapter choice determines **usage normalisation**, not just routing. Picking the wrong
one produces plausible but wrong cost numbers. If unsure, run once and compare
`usage/provider_raw.json` against the provider's own billing dashboard.

Claude Code speaks the Anthropic Messages API only, so an OpenAI-compatible provider needs
a shim in front of it. That shim is part of the system under test — see
[`claude-code-model-replacement.md`](claude-code-model-replacement.md).

## Comparing the same model across deployments

Give each deployment its own target with the same `model_family`:

```yaml
  glm-fireworks:      { model_family: glm, provider: fireworks, deployment_type: api, ... }
  glm-self-hosted:    { model_family: glm, provider: internal,  deployment_type: self_hosted, ... }
```

Reports can then group by `model_family` (is the model good enough?) or by target (which
deployment is economically better?) — the two questions this project has to answer
separately.
