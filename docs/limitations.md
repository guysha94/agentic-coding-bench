# Limitations

What this benchmark cannot tell you. Read this before quoting a number in a decision.

---

## 1. It measures the model *inside Claude Code*, not the model

Claude Code's system prompts, tool schemas, agent loop and compaction were designed and
tuned against Anthropic models. A low score for GLM is evidence about
**GLM-inside-Claude-Code**, not about GLM in general — the same weights behind a different
harness could score very differently.

This is the right question for *our* decision (we are deciding what to run inside Claude
Code) and the wrong question for a general model ranking. Do not use these results to claim
one model is better than another.

## 2. A translating proxy is part of the system under test

Claude Code speaks the Anthropic Messages API only, so any OpenAI-shaped provider sits
behind a shim. The harness measures model + shim jointly and **cannot separate them**.

Mitigation: transport-sensitive signals (failed tool calls with malformed input, missing
cache fields, non-`end_turn` stop reasons, zero tool calls) are reported separately from
competence signals, so a shim bug is at least *visible*. But attributing a specific failure
to the proxy versus the weights requires manual inspection of `usage/provider_raw.json` and
`tool_calls.jsonl`.

## 3. Prompt-cache behaviour moves cost more than quality

A single trivial run was observed writing **188k cache-creation tokens**. On agentic
workloads, cache behaviour dominates the bill. If a provider or proxy ignores Anthropic
cache breakpoints, an otherwise cheap model becomes expensive — and if it fails to *report*
cache tokens, the cost is unknowable rather than zero.

The framework marks such runs `usage.complete = false` and `cost.incomplete = true`, and
reports cache-inclusive and cache-exclusive figures. Read the warnings.

## 4. Wall clock is not a clean measurement

It includes provider queueing, shared-tenant latency, local CPU contention during test runs,
and disk speed. It is machine-dependent and not comparable across machines. This is why time
carries only 10% of the composite score, and why parallel execution is off by default.

## 5. Small N

The proof-of-concept is 10 tasks. At `--runs 3` that is 30 runs per target — enough to
detect large differences (say, 90% vs 50% success), not enough to resolve small ones.
Wilson intervals are reported for exactly this reason: 4/4 successes shows as **51–100%**,
because that is the honest range.

Do not read a 2-point score gap as a finding.

## 6. Hidden tests reduce but do not eliminate overfitting

The agent can read the repository it is fixing, and can often infer what a reasonable test
would check. Hidden tests raise the bar (they target non-obvious rules and side effects)
without making gaming impossible.

## 7. Fixture realism is limited

Two small fixture repositories (~1.5k LOC combined). That constrains two categories in
particular:

- **`large_context`** currently means "read this whole small repo", not "navigate 50k LOC".
- **Language coverage** is Python plus YAML/shell. Results may not transfer to TypeScript,
  Go, or Rust.

Both are addressed by adding fixtures — see `methodology.md` §7.

## 8. MCP coverage is synthetic

The bundled MCP server is deliberately local and fixed, which buys reproducibility at the
cost of realism: no auth flows, no pagination, no rate limits, no flaky upstreams, no
sprawling tool catalogues (a real workspace exposed **258 tools**, and tool-selection
difficulty scales with catalogue size). Real-MCP tasks are supported via
`requires_network: true` but are inherently less reproducible.

## 9. Subscription cost allocation is an assumption

There is no correct per-task cost for a monthly subscription. The framework reports the raw
monthly cost plus an allocation under a named strategy, always labelled an estimate.
Comparing a subscription target against per-token targets on cost requires stating the
assumption out loud.

## 10. Break-even analysis omits concurrency

Observed throughput is *sequential single-session* throughput, and benchmark wall clock
includes agent thinking, tool execution and test runs. A real inference server batches many
sessions, so actual throughput is higher and the true break-even point is **lower** than
reported. Treat the figure as a conservative bound.

## 11. Metrics we cannot capture

| Wanted | Why not |
|---|---|
| Token-level attribution per tool call | The CLI reports usage per assistant message, not per tool call. Per-message usage is captured; finer attribution is not available. |
| True context-window utilisation over time | Not exposed per turn. `contextWindow` is reported in `modelUsage`, and turn counts serve as a proxy. |
| Server-side compaction events | Compaction is not surfaced as a distinct stream event; turn count and token growth are indirect signals. |
| GPU utilisation during self-hosted runs | Requires instrumentation on the inference server, outside this harness. `utilization_assumption` is a configured input, not a measurement. |
| Reasoning tokens for providers that don't report them | Absent means absent; recorded as 0 with `complete = false`. |
| Whether a model "understood" the architecture | Only its consequences are observable — hence hidden tests that assert *where* a rule was placed, not that the agent claimed to understand. |

## 12. What a passing score does not prove

A run that satisfies every gate has passed *these* tests on *this* commit. It does not
prove the change is production-ready, idiomatic, secure, performant, or well-designed. Lint
and type-check are weak proxies for code quality, and there is deliberately no
"looks good to me" score, because a subjective grader would not be reproducible.

Read `git.diff` before concluding a target is good enough to ship behind.
