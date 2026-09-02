# Agentic Coding Benchmark — Design Document

**Status:** implemented (v0.1.0)
**Framework version:** `acb` 0.1.0
**Last updated:** 2026-09-02

---

## 1. Research question

> Can we replace Anthropic Claude models (Opus/Sonnet) inside Claude Code with a
> self-hosted or API-hosted open-weight model (GLM, Kimi, …) while preserving most of
> the coding productivity, agent/tool-use quality, and reliability, at a significantly
> lower **effective** cost?

The unit of evaluation is **not** the model. It is the pair
`(model, orchestration environment)` — i.e. the complete agentic coding experience that
Claude Code provides: system prompts, agent loop, Bash/tool usage, file editing, search,
planning, git operations, MCP servers, context management, error recovery, and iterative
debugging.

Consequently the benchmark drives the **real `claude` CLI** as a subprocess and swaps only
the model/provider underneath it. Everything else in the harness is held constant.

---

## 2. Hypotheses

| # | Hypothesis | How the benchmark tests it |
|---|---|---|
| H1 | Open-weight models reach a materially lower task success rate than Opus on multi-step agentic tasks, but a comparable rate on single-file, well-specified tasks. | Success rate broken down by `category` and `difficulty`. |
| H2 | The gap is driven more by *agentic* failure (tool-call errors, loops, giving up, context loss) than by raw code-writing ability. | Tool-call metrics: `failed_tool_calls`, `repeated_reads`, `tool_calls_per_success`, timeout rate, and the recorded tool sequence. |
| H3 | Cost-per-*successful*-task ranks targets differently from cost-per-token; a cheap model that fails often can be more expensive in practice. | `cost_per_successful_task` vs raw token price, per target. |
| H4 | MCP / external-tool workflows degrade disproportionately on non-Anthropic models, because tool-choice behaviour is tuned to Anthropic conventions. | Category `mcp` scores + MCP-specific metrics (correct server, correct tool, redundant calls). |
| H5 | Self-hosting beats API pricing only above a workload threshold that depends on assumed GPU utilisation. | Break-even analysis from observed throughput + configured infra cost. |

Hypotheses are stated so they can be **falsified**. The framework reports raw metrics
alongside composite scores precisely so a single number cannot hide a rejected hypothesis.

---

## 3. Prior art and what we borrow

| Source | What we borrow | What we deliberately change |
|---|---|---|
| **SWE-bench** | Objective, test-based grading (FAIL→PASS / PASS→PASS); hidden tests held out from the agent; per-instance isolated checkout. | SWE-bench grades a *patch*. We grade an *agent session* — the trajectory matters as much as the diff, and the agent runs the tests itself. |
| **SWE-agent** | Trajectory logging as a first-class artifact; the agent-computer interface is part of what's measured. | Our ACI is fixed (it is Claude Code). We vary the model beneath it instead of designing the interface. |
| **Terminal-Bench** | Container/terminal tasks with scripted verification; timeouts as a first-class outcome; "did the end state satisfy the check" rather than "did it look right". | We use git worktrees rather than per-task containers by default (much faster, adequate for our threat model), with containers as an opt-in escalation. |
| **HumanEval / MBPP** | — | Explicitly **not** used: single-function synthesis says nothing about agentic tool use. |

We adapt rather than copy: the object of study is *real developer workflow inside Claude
Code*, so grading combines (a) objective validation commands, (b) regression checks
against a pre-run baseline, and (c) trajectory/efficiency metrics.

---

## 4. Empirical findings about the Claude Code CLI

These were **measured** on this machine (`claude 2.1.252`, macOS/darwin 25.6.0), not assumed.
Findings vs. assumptions are separated; see `docs/claude-code-model-replacement.md` for the
full §31 investigation including everything still labelled *unknown*.

### 4.1 What we can capture (verified)

`claude -p --output-format stream-json --verbose` emits newline-delimited JSON. Observed
event types and the fields we depend on:

| Event | Fields we consume |
|---|---|
| `system/init` | `session_id`, `cwd`, `model`, `permissionMode`, `tools[]`, `mcp_servers[]` (name+status), `slash_commands[]`, `agents[]`, `skills[]`, `plugins[]`, `claude_code_version`, `apiKeySource`, `output_style` |
| `assistant` | `message.content[]` (`text` / `thinking` / `tool_use{id,name,input}`), `message.usage{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens, cache_creation.ephemeral_{5m,1h}_input_tokens, service_tier}`, `message.model`, `timestamp`, `uuid`, `request_id`, `parent_tool_use_id` |
| `user` | `message.content[]` (`tool_result{tool_use_id, content, is_error}`), `timestamp` |
| `result` | `duration_ms`, `duration_api_ms`, `ttft_ms`, `num_turns`, `total_cost_usd`, `usage{…, output_tokens_details.thinking_tokens, iterations[]}`, `modelUsage{<model>:{inputTokens, outputTokens, cacheReadInputTokens, cacheCreationInputTokens, costUSD, contextWindow, provider, costBasis}}`, `permission_denials[]`, `terminal_reason`, `is_error`, `subagent_stats` |
| `rate_limit_event` | rate-limit utilisation windows (recorded, used to explain outliers) |
| `system/hook_*` | hook lifecycle (recorded when present) |

This gives us, per run: the **complete ordered tool-call sequence with timestamps**, per-call
success/failure, per-message token usage by class, model identity, and wall-clock timing.
That is substantially richer than the "proxy measurement" the brief allowed for — no
log-scraping heuristics are needed.

### 4.2 Cost fields are NOT trustworthy across targets (verified)

`result.total_cost_usd` and `modelUsage[].costUSD` are computed by Claude Code from its own
first-party price table (`costBasis: "list"`, `provider: "firstParty"`). For a proxied or
self-hosted model this number is meaningless — it prices foreign tokens at Anthropic list
rates, or reports `0`.

**Design consequence:** the framework never uses `total_cost_usd` for comparison. It
recomputes every cost from **raw token counts** through its own configuration-driven cost
engine. `total_cost_usd` is preserved in the raw artifacts as a cross-check only.

### 4.3 Ambient developer config leaks into sessions (verified, and mitigated)

A default `claude -p` run on this machine exposed **258 tools, 20 MCP servers, 234 slash
commands, and dozens of plugins/skills** inherited from the developer's global config. Any
of these would silently change agent behaviour between runs and between machines.

Verified mitigation — this exact combination yields a hermetic session
(**2 tools, 0 MCP servers, 0 slash commands, 0 plugins, 0 skills**):

```
CLAUDE_CONFIG_DIR=<per-run scratch dir> \
claude -p --strict-mcp-config --mcp-config <benchmark mcp.json> \
       --setting-sources "" --disable-slash-commands \
       --tools "<explicit list>" ...
```

**Design consequence:** the runner *always* builds a hermetic config dir and passes these
flags. The set of available tools becomes an explicit, recorded experimental parameter
rather than an accident of the operator's laptop.

### 4.4 Auth does not survive config-dir isolation (verified)

With a fresh `CLAUDE_CONFIG_DIR`, a subscription (OAuth) login is not visible and the run
fails with `Not logged in · Please run /login` (`terminal_reason: "api_error"`). Credentials
must therefore be injected explicitly per target via environment
(`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ANTHROPIC_BASE_URL`), or the operator must
opt out of config-dir isolation.

**Design consequence:** `benchmark doctor` checks credential availability per target
*before* a suite starts, and targets declare which env vars they require. Subscription-auth
targets are supported via an explicit `isolate_config_dir: false` escape hatch, with the
reproducibility caveat recorded in the run metadata.

### 4.5 Non-Anthropic routing mechanism

Claude Code reaches a non-Anthropic model through an **Anthropic-Messages-compatible**
endpoint (`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`), or through the first-party
Bedrock/Vertex/Foundry integrations. An OpenAI-shaped provider therefore requires a
translating proxy in front of it. The consequences of that translation layer
(tool-call fidelity, cache reporting, thinking blocks) are the single largest source of
confounding in this experiment and are enumerated as *unknowns* in
`docs/claude-code-model-replacement.md` §3 rather than guessed at here.

---

## 5. Experimental methodology

### 5.1 The controlled variable

The **only** intended difference between two runs of the same task is the
**benchmark target**:

```
target = model × provider × deployment × pricing model
```

Held constant by construction: repository and base commit, task prompt (byte-identical),
MCP configuration, available tool list, permission mode, timeout, context/autocompact
policy, environment variables (except the provider block), working directory layout,
and validation commands. Every run starts from a **fresh session** with no memory of prior
runs (a fresh `CLAUDE_CONFIG_DIR`, a fresh `--session-id`, and no `--resume`/`--continue`).

### 5.2 Run protocol

```
1.  resolve task + target + pricing  ──►  freeze a RunSpec
2.  create isolated workspace (git worktree @ base_commit)
3.  assert clean tree; record environment fingerprint
4.  run *baseline* validation  (to distinguish pre-existing failures from regressions)
5.  execute claude -p in the workspace, streaming NDJSON to disk
6.  on timeout: SIGTERM → grace → SIGKILL; record partial artifacts, outcome=timeout
7.  capture git diff + changed-file list
8.  run *post* validation (visible commands, then hidden tests)
9.  normalize usage → compute cost → score
10. persist run row + artifacts; destroy workspace
```

Steps 4 and 8 use identical commands, so `regression_count` is computed as
"validation items that passed at baseline and fail after the agent ran". This is what makes
"fixed the bug without breaking anything" objectively checkable.

### 5.3 Repetition

Model behaviour is stochastic, so a single run is evidence of almost nothing. Every
`(task, target)` cell is run `--runs N` times (default 3). Runs are stored independently and
never overwritten; aggregation reports mean, median, stdev, p50/p90, and success rate with a
Wilson score interval (which is honest at small N, unlike a naive proportion).

### 5.4 Ordering and drift control

Runs are executed **target-major within a repetition round** (round 1: all targets × all
tasks; round 2: …) rather than finishing one target before starting the next. This spreads
provider-side drift (rate limits, capacity, model updates) roughly evenly across targets
instead of concentrating it in whichever target ran last.

---

## 6. Benchmark categories

| Key | Category | What it isolates |
|---|---|---|
| `bug_fix` | Bug fixing | Reproduce → root-cause → minimal fix → verify. |
| `feature` | Feature implementation | Multi-file change with objective tests. |
| `refactor` | Refactoring | Behaviour-preserving restructuring; validated by unchanged tests + static analysis. |
| `repo_understanding` | Repository understanding | Navigate a non-trivial codebase; change the *correct layer*. |
| `large_context` | Large-context tasks | Many-file inspection; measures context efficiency and whether earlier discoveries are lost. |
| `debugging` | Debugging | Given logs/stack traces/failing integration tests, investigate and fix. |
| `devops` | Terminal / DevOps | Dockerfile, compose, CI, shell automation — Terminal-Bench-shaped. |
| `mcp` | MCP / external tool usage | Correct server, correct tool, no redundant calls, information correctly incorporated. |
| `multi_step` | Multi-step agentic | Plan → investigate → implement → test → debug → verify. |

Every category is a plain string in the task file; adding a category requires no code change
(the enum is open — unknown categories are accepted and reported).

---

## 7. Metrics

Full definitions live in `docs/metrics.md`. Summary of what is captured and how:

**Outcome** — success (all gates pass), test pass rate, hidden-test pass rate, build,
lint, type-check, regression count. *Derived from validation command exit codes + parsed
test counts.*

**Time** — total wall-clock, API time, time-to-first-token, **time to first code
modification** (timestamp of the first successful `Edit`/`Write`/`NotebookEdit`/mutating
`Bash` minus session start), time to first successful validation, timeout rate.

**Agent tool use** — total calls; per-kind counts (bash, read, write/edit, search, git,
MCP, web, task/subagent); failed calls (`tool_result.is_error`); repeated calls (identical
name+normalised input); repeated *reads* of the same path; the **full ordered sequence**
with timestamps, persisted to `tool_calls.jsonl` and summarised as a compact string
(`Search→Read→Read→Bash→Edit→Bash`).

**Model usage** — input, output, cached-input, cache-write, reasoning/thinking tokens, and
turn count, normalised across providers (§9).

**Efficiency (derived)** — tool calls / wall time / tokens / cost / MCP calls per
*successful* task; failed calls per run; repeated reads per run; context churn.

**Unavailable metrics** are documented explicitly in `docs/limitations.md` rather than
approximated silently.

---

## 8. Architecture

Strictly layered; each arrow is a one-way dependency. No layer imports from below it.

```
config/targets.yaml ── TargetConfig ─┐
tasks/*.yaml ───────── TaskSpec ─────┤
                                     ▼
                            Provider/Model Adapter        (env + argv + usage dialect)
                                     ▼
                              Agent Runner                (claude -p subprocess)
                                     ▼
                          Workspace Isolation             (git worktree)
                                     ▼
                          Telemetry / Logging             (stream-json → events)
                                     ▼
                          Usage Normalization             (→ NormalizedUsage + raw)
                                     ▼
                              Cost Engine                 (token|infra|subscription)
                                     ▼
                               Validation                 (baseline vs post)
                                     ▼
                                Scoring                   (weighted, configurable)
                                     ▼
                           Database (SQLite→PG)
                                     ▼
                          Reporting (HTML/JSON/CSV)
```

**Key decoupling rule:** a task file never names a model, provider, price, or endpoint; a
target file never names a task. Adding a provider, model, or price is a YAML change with no
task edits and no code change — unless the provider needs a *new usage dialect*, which is
the one legitimate reason to add an adapter (§9).

---

## 9. Normalized usage model and double-counting

Providers disagree about whether cached tokens are *inside* or *beside* the input count.
Getting this wrong silently corrupts every cost number, so normalisation is an explicit,
versioned, per-adapter responsibility.

```python
NormalizedUsage(
    input_tokens,          # billable, non-cached prompt tokens
    output_tokens,
    cached_input_tokens,   # cache READ (discounted)
    cache_write_tokens,    # cache WRITE (often a premium)
    reasoning_tokens,      # thinking, when separately exposed
    total_turns,
)
```

| Dialect | Provider convention | Normalisation |
|---|---|---|
| `anthropic_v1` | `input_tokens` **excludes** cache read & creation | `input = input_tokens` (pass-through) |
| `openai_compat_v1` | `prompt_tokens` **includes** `prompt_tokens_details.cached_tokens` | `input = prompt_tokens − cached_tokens` (clamped ≥ 0) |

`reasoning_tokens` are treated as **already contained in** `output_tokens` (Anthropic's
`output_tokens_details.thinking_tokens` is a subset). They are priced separately only if the
target configures a distinct `reasoning_per_million_tokens`, in which case they are
subtracted from the output count first. The rule is asserted in tests so it cannot silently
regress.

Every run stores **both** `usage/normalized.json` and `usage/provider_raw.json`, plus the
`normalization_version` used. Raw usage is never discarded, so a normalisation bug can be
corrected retroactively without re-running the benchmark.

---

## 10. Cost model

Four pricing types, all configuration-driven. **No price is ever hardcoded in source.**

1. **`token`** — per-million rates for input / output / cached-input / cache-write /
   reasoning. Missing rates are `null`, not `0`: a `null` rate means *unknown* and the run's
   cost is flagged `incomplete`, rather than silently understating cost as free.
2. **`infrastructure`** — GPU count × hourly rate + additional machine cost, over the run's
   wall-clock hours. Produces **two** figures that are never mixed:
   - *marginal run cost* = `runtime_h × (gpu_count × gpu_hourly + machine_hourly)`
   - *effective cost* = `marginal / utilization_assumption` — what the capacity actually
     costs given that an idle GPU still bills.
   Extensible hooks are reserved for minimum billing intervals, spot/reserved discounts,
   storage, and network.
3. **`subscription`** — a monthly cost plus an explicit `allocation_strategy`
   (`active_user` / `observed_usage_share` / `working_hours` / `manual`). Subscription
   economics are genuinely ambiguous, so the report shows the raw monthly cost **and** the
   allocated per-task estimate, always labelled an estimate with its assumption named.
4. **`unknown`** — cost is `null` and excluded from cost aggregates. The target still
   competes on quality. This is the honest default for a provider whose pricing is not yet
   settled.

The headline metric is **cost per successful task**, not cost per token — a cheap model that
fails half the time can be the more expensive option.

### Historical reproducibility

Every run persists a `pricing_snapshot.json` (provider, model, target, pricing type, all
rates, currency, effective date, normalisation version). Reports price historical runs with
**the snapshot taken at run time**. `benchmark report --reprice-current` re-prices them
against today's configuration as an explicit, opt-in what-if.

---

## 11. Isolation strategy

**Default: git worktrees.** For each run, `git worktree add --detach <tmp> <base_commit>`
from the fixture repo. This is fast (no re-clone, shared object store), gives a genuinely
clean tree at an exact commit, and is trivially parallelisable. The workspace is verified
clean before the agent starts and destroyed afterwards (`git worktree remove --force` +
prune).

Escalation path, in order: worktree → temporary full clone (`clone_temp`, for tasks that
mutate git state) → container (`container`, reserved for hostile or dependency-heavy tasks).

**Process isolation:** each run gets a scratch `CLAUDE_CONFIG_DIR`, a filtered environment
(allowlist, not the operator's shell), `cwd` pinned to the workspace, and an explicit tool
allowlist. The agent is not handed the operator's credentials — only the target's.

**Concurrency:** runs are independent by construction (separate worktrees, separate config
dirs, separate DB rows). Parallel execution is therefore safe for correctness, but is
**off by default** because concurrent runs contend for CPU and distort wall-clock timing —
which is a scored metric.

---

## 12. Scoring

Composite score in [0, 1], weights configurable in `config/scoring.yaml`:

| Dimension | Default weight | Basis |
|---|---|---|
| Functional correctness | 0.50 | visible tests, hidden tests, build, required-functionality gates, minus regressions |
| Code quality | 0.15 | lint, type-check, static analysis (only dimensions the task actually configures) |
| Agent efficiency | 0.15 | tool-call count vs task budget, failed-call rate, repeated-read rate |
| Time | 0.10 | normalised against the task timeout |
| Cost | 0.10 | normalised across targets in the comparison set |

Two deliberate rules:
- **Cost and time are normalised *within a comparison*,** not against absolute constants, so
  the composite stays meaningful as prices change.
- **A composite score is never reported alone.** Every view that shows a score also shows
  the raw metrics behind it, because a single number can hide exactly the trade-off this
  project exists to surface.

Dimensions a task doesn't configure (e.g. no type-checker) are dropped and the remaining
weights re-normalised, so tasks aren't penalised for absent tooling.

---

## 13. Data model

SQLite first, schema written to port cleanly to PostgreSQL (integer surrogate keys, no
SQLite-specific types, timestamps as ISO-8601 UTC text, JSON payloads in TEXT columns).
Forward-only numbered migrations tracked in a `schema_migrations` table.

```
models ── providers ── benchmark_targets ── pricing_configs
                              │
benchmark_tasks ── benchmark_suites (M:N via suite_tasks)
                              │
                        benchmark_runs ──┬── tool_events
                                         ├── validation_results
                                         ├── run_metrics
                                         ├── model_usage
                                         └── pricing_snapshots
```

`benchmark_runs` is the fact table; everything else hangs off it. Artifacts live on disk
under `runs/<run-id>/` and are referenced by path, not inlined as blobs.

---

## 14. Known limitations

Stated up front, expanded in `docs/limitations.md`:

1. **Claude Code's own scaffolding is a confounder.** Its prompts and tool descriptions are
   tuned on Anthropic models. A low GLM score is evidence about *GLM-inside-Claude-Code*,
   not about GLM in general. This is the right question for our decision but the wrong
   question for a general model ranking.
2. **A translating proxy is part of the system under test.** Any OpenAI-compatible target
   measures model + proxy jointly; they cannot be separated by this harness.
3. **Prompt-cache behaviour differs by provider**, which moves cost more than it moves
   quality. Cache-inclusive and cache-exclusive cost are both reported.
4. **Wall-clock is machine- and load-dependent**, and shared-tenant API latency is not under
   our control. Time is deliberately weighted only 10%.
5. **Small N.** 5–10 tasks × 3 runs detects large differences, not subtle ones. Confidence
   intervals are reported so nobody over-reads a 2-point gap.
6. **Hidden tests reduce but don't eliminate overfitting**, since the agent can read the
   repository it is fixing.
7. **MCP tasks depend on live external services**, so they are inherently less reproducible;
   they are marked `requires_network` and can be excluded from headline numbers.
8. **Subscription cost allocation is an assumption, not a measurement.**

---

## 15. Implementation phases

| Phase | Scope | Status |
|---|---|---|
| 1 | Research + design (this document, §31 findings) | done |
| 2 | Core: task spec, targets, providers, pricing, DB, workspace isolation | done |
| 3 | Agent execution: `claude -p` driver, artifact capture, timeout handling | done |
| 4 | Telemetry + cost: stream-json parsing, usage normalisation, cost engine | done |
| 5 | Evaluation: validation, regression detection, scoring, metrics | done |
| 6 | Comparison: repeated runs, aggregation, statistics | done |
| 7 | Reporting: HTML/JSON/CSV, cost-vs-quality, break-even | done |
| 8 | Initial suite: 8 tasks across 7 categories + fixtures, dry runs | done |

Each phase ends with tests green and documentation updated.

---

## 16. Safety / threat model

The framework hands an LLM a shell. Full threat model in `docs/threat-model.md`. Controls:
workspace-confined execution with `cwd` pinned to a throwaway worktree; a filtered
environment allowlist so developer and production credentials are never inherited; explicit
per-target credential injection; secret redaction on every persisted artifact; a refusal to
run against a dirty or non-fixture repository; and no destructive host-level commands issued
by the harness itself. The agent is *deliberately* permitted to run arbitrary commands
**inside** the workspace — that is the thing being measured — which is why the workspace is
disposable and the environment is minimal.
