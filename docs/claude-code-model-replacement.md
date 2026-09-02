# Claude Code Model Replacement — Findings vs. Unknowns

This document answers §31 of the brief: *what actually happens when Claude Code's normal
Anthropic model is replaced or proxied by another model?*

It is deliberately split into three sections:

- **§1 Verified** — observed on this machine, with the command that produced the observation.
- **§2 Documented** — stated by the CLI's own interface (flags, help text, error messages).
- **§3 Unknown** — *not* observed, because no non-Anthropic endpoint was reachable from this
  environment. These are labelled unknown rather than guessed, and the framework is built to
  **measure** them once an endpoint exists.

Environment: `claude 2.1.252`, macOS (darwin 25.6.0), Python 3.13.13.

---

## 1. Verified observations

### 1.1 Telemetry is rich and machine-readable

`claude -p --output-format stream-json --verbose` emits NDJSON containing a `system/init`
event, one `assistant` event per model message (with `tool_use` blocks and per-message
`usage`), one `user` event per tool result (with `is_error`), and a terminal `result` event.

Verified consequence: **the complete tool-call sequence, per-call success/failure,
per-message token usage by class, and wall-clock timing are all directly observable.** No
log-scraping heuristics are required. The brief's fallback ("if exact tool-call extraction
is not possible, document the limitation") does not apply.

### 1.2 Reported cost is first-party-only

Observed in a `result` event:

```json
"modelUsage": { "claude-sonnet-5": {
    "costUSD": 0.7524, "provider": "firstParty", "costBasis": "list" } }
```

`total_cost_usd` / `costUSD` are computed from Claude Code's **own** price table. For a
proxied model this is either wrong (foreign tokens priced at Anthropic list rates) or `0`.

**Consequence:** the benchmark recomputes all cost from raw token counts. `total_cost_usd`
is preserved as a cross-check, never used for comparison.

### 1.3 Token accounting survives independently of the model

Token classes (`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`, `output_tokens_details.thinking_tokens`) are reported by the CLI
in the Anthropic Messages shape. Whether a **proxy** populates them faithfully is §3.1.

### 1.4 Ambient config leaks, and can be fully suppressed

A default run inherited **258 tools, 20 MCP servers, 234 slash commands**, plus plugins and
skills from the operator's global configuration.

Verified suppression — `CLAUDE_CONFIG_DIR=<scratch> … --strict-mcp-config --mcp-config
<file> --setting-sources "" --disable-slash-commands --tools "Read,Bash"` produced exactly
**2 tools, 0 MCP servers, 0 slash commands, 0 plugins, 0 skills**.

**Consequence:** without this, "same available tools" across targets is not actually true,
and results would be contaminated by whichever laptop ran them.

### 1.5 Config-dir isolation drops subscription auth

With a fresh `CLAUDE_CONFIG_DIR`, the run terminated with
`result: "Not logged in · Please run /login"`, `terminal_reason: "api_error"`,
`total_cost_usd: 0`.

**Consequence:** credentials must be injected per target via environment, or config-dir
isolation must be explicitly disabled for subscription-auth targets (supported, with the
reproducibility caveat recorded in run metadata).

### 1.6 Permission mode materially changes the trajectory

A `Read` outside the trusted directory produced
`tool_result{is_error: true, content: "Claude requested permissions to read…"}` and the
agent abandoned the task. Permission policy is therefore an experimental parameter that must
be identical across targets — an accidental difference would look exactly like a model
quality difference.

---

## 2. Documented by the CLI interface

- **Routing to a non-Anthropic model** goes through an Anthropic-Messages-compatible
  endpoint (`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`), or the first-party
  Bedrock / Vertex / Foundry integrations. There is no OpenAI-shaped client inside Claude
  Code — an OpenAI-compatible provider needs a translating proxy.
- **The system prompt is fixed unless overridden.** `--system-prompt`,
  `--append-system-prompt`, and `--exclude-dynamic-system-prompt-sections` exist; the
  benchmark uses **none** of them, so all targets receive Claude Code's default system
  prompt. (`--exclude-dynamic-system-prompt-sections` is noted as a future knob for
  cross-machine cache reuse.)
- **Context management is configurable** via `--autocompact <auto|tokens>`. The benchmark
  pins it identically across targets so compaction behaviour is not a free variable.
- **Effort/thinking** is exposed as `--effort {low,medium,high,xhigh,max}`. This is an
  Anthropic-model concept; how a proxy handles it is §3.4.
- **Budget guard**: `--max-budget-usd` exists but is priced with the first-party table
  (§1.2), so it is unreliable for non-Anthropic targets and is not used as the primary
  timeout/abort mechanism. Wall-clock timeout is authoritative.

---

## 3. Unknown — to be measured, not assumed

Each item names the observation that would resolve it. The framework records the data needed
to answer all of them on the first real non-Anthropic run.

### 3.1 Does a proxy report token usage faithfully?
Unknown whether `cache_creation_input_tokens` / `cache_read_input_tokens` are populated,
zeroed, or fabricated by a translating proxy.
*Resolved by:* comparing `usage/provider_raw.json` against the proxy's own billing. If cache
fields are absent, cost is flagged `incomplete` rather than assumed zero.

### 3.2 Are tool definitions transmitted identically?
Claude Code sends the same tool schemas regardless of target, but whether a proxy preserves
JSON-Schema fidelity (nested objects, unions, long descriptions) is unknown.
*Resolved by:* `failed_tool_calls` with malformed-input errors, captured per run.

### 3.3 Does tool-choice behaviour change?
Strongly suspected but unmeasured: non-Anthropic models may over- or under-call tools,
prefer `Bash` over structured `Edit`, or fail to emit parallel tool calls.
*Resolved by:* the recorded tool sequence and per-kind counts, compared across targets on
identical tasks — which is the core of hypothesis H2.

### 3.4 Does thinking/reasoning configuration behave differently?
Unknown whether `--effort` maps onto anything for a proxied model, and whether reasoning
tokens are reported separately, folded into output, or dropped.
*Resolved by:* `output_tokens_details.thinking_tokens` vs the provider's raw usage.

### 3.5 Does context management / compaction differ?
Compaction is Claude Code's logic, so it should be identical — but it is *triggered* by token
counts the provider reports, and a provider that miscounts will compact at the wrong time.
*Resolved by:* comparing turn counts and context-window utilisation on `large_context` tasks.

### 3.6 Does prompt caching work at all?
Anthropic-style cache breakpoints may be ignored by a proxy, silently converting a cached
workload into a full-price one. Given that the observed run wrote **188k cache-creation
tokens for a trivial prompt**, this dominates cost.
*Resolved by:* `cached_input_tokens` > 0 across turns; the report shows cache-inclusive and
cache-exclusive cost side by side.

### 3.7 Is MCP behaviour preserved?
MCP tool invocation is Claude Code's mechanism, so plumbing should be identical; whether the
model *selects* MCP tools correctly is exactly what the `mcp` category measures.
*Resolved by:* MCP category success rate + correct-server/correct-tool metrics.

### 3.8 Do unsupported API semantics cause silent degradation?
Unknown which Messages-API features (parallel tool use, `stop_reason` variants, streaming
partial JSON, `service_tier`) a given proxy implements.
*Resolved by:* `terminal_reason`, `api_error_status`, and non-`end_turn` stop reasons, all
persisted per run.

---

## 4. Bottom line for the experiment

Claude Code's orchestration — system prompt, agent loop, tool schemas, compaction, MCP
plumbing, permission gating — is **client-side and identical across targets**. What varies is
(a) the model's competence at using it, and (b) the fidelity of whatever transport sits
between the CLI and the weights.

The benchmark therefore measures `model × transport` inside a fixed harness, and reports
transport-sensitive signals (cache tokens, failed tool calls, malformed input, non-standard
stop reasons) separately from competence signals (task success, regressions, efficiency) — so
a proxy bug is not mistaken for a bad model.
