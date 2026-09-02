# agentic-coding-bench

A benchmarking framework for evaluating coding models **inside Claude Code** — across
agentic workflows, tool use, quality, performance, and cost.

It exists to answer one question with evidence rather than vibes:

> Can we replace Claude Opus/Sonnet inside Claude Code with a self-hosted or API-hosted
> open-weight model (GLM, Kimi, …) while preserving most of the coding productivity,
> agent/tool-use quality, and reliability, at a significantly lower **effective** cost?

The unit of evaluation is not the model in isolation — it is the model *driving the real
`claude` CLI*, with the same prompts, tools, MCP servers, agent loop, and error recovery.
Only the model/provider changes between runs.

---

## Quick start

```bash
uv sync
```

```bash
uv run benchmark init      # unpacks fixture repos from bundles, creates the database
```

```bash
uv run benchmark doctor
```

```bash
uv run benchmark run --suite smoke --targets claude-opus-anthropic,glm-fireworks --runs 3
```

```bash
uv run benchmark report
```

`doctor` will tell you what is missing before you spend anything: credentials, fixtures,
a dirty fixture repo, incomplete pricing.

> **Before your first real run:** every price in `config/targets.yaml` is a `0.00`
> placeholder. Fill them in from your provider's current price list. The framework never
> hardcodes prices, and a `null` rate means *unknown* — runs priced with one are flagged
> `incomplete` rather than reported as cheap.

---

## What it measures

| | |
|---|---|
| **Outcome** | task success, visible + hidden test pass rate, build, lint, type-check, regressions |
| **Time** | wall clock, API time, time-to-first-token, time to first code modification, timeout rate |
| **Agent tool use** | total calls, per-kind counts, failed calls, repeated calls, repeated file reads, and the full ordered tool sequence with timestamps |
| **Model usage** | input / output / cached-input / cache-write / reasoning tokens, turn count — normalised across providers |
| **Cost** | token, self-hosted infrastructure, or subscription pricing; headline metric is **cost per successful task** |
| **MCP** | correct server, correct tool, redundant calls, whether external information reached the implementation |

Everything is recomputed from raw token counts by our own cost engine. Claude Code's own
`total_cost_usd` is stored as a cross-check but never used for comparison — it prices
foreign tokens at Anthropic list rates (see [`docs/claude-code-model-replacement.md`](docs/claude-code-model-replacement.md)).

---

## How it works

```
config/targets.yaml ── TargetConfig ─┐
tasks/*.yaml ───────── TaskSpec ─────┤
                                     ▼
                      Provider/Model Adapter   env wiring + usage dialect
                                     ▼
                          Agent Runner         claude -p, streaming NDJSON
                                     ▼
                      Workspace Isolation      git worktree @ exact commit
                                     ▼
                      Telemetry / Logging      stream-json -> tool events
                                     ▼
                      Usage Normalization      -> NormalizedUsage + raw
                                     ▼
                          Cost Engine          token | infra | subscription
                                     ▼
                           Validation          baseline vs post
                                     ▼
                            Scoring            weighted, configurable
                                     ▼
                     Database (SQLite -> PG)
                                     ▼
                     Reporting (HTML/JSON/CSV)
```

A task file never names a model, provider, endpoint or price. A target file never names a
task. Adding a provider, model or price is a YAML change.

### Every run is isolated

Each run gets a fresh `git worktree` at an exact commit, a scratch `CLAUDE_CONFIG_DIR`, an
allowlisted environment, and an explicit tool list. Verified: this reduces a session from
the operator's ambient **258 tools / 20 MCP servers / 234 slash commands** to exactly what
the benchmark declares. Without it, "same available tools" is not actually true.

### Every run is reproducible

Each run persists:

```
runs/<run-id>/
  metadata.json          prompt.txt        agent.log
  stdout.log             stderr.log        tool_calls.jsonl
  model_usage.json       git.diff          changed_files.json
  validation.json        score.json        pricing_snapshot.json
  usage/normalized.json  usage/provider_raw.json
```

Raw provider usage is never discarded, so a normalisation bug can be fixed retroactively
without re-running anything. Reports price historical runs with the snapshot taken *at run
time*; `benchmark report --reprice-current` is an explicit opt-in what-if.

---

## Commands

```bash
benchmark init                       # unpack fixtures, create database, register config
benchmark fixtures [--refresh|--force] # manage fixture repositories
benchmark doctor                     # verify the environment before spending money
benchmark validate                   # check task/target config without running
benchmark list tasks|targets|suites
```

```bash
benchmark run --task payments-bug-001 --target claude-opus-anthropic
```

```bash
benchmark run --suite backend --targets claude-opus-anthropic,glm-fireworks,kimi-fireworks --runs 3
```

```bash
benchmark compare --targets claude-opus-anthropic,glm-fireworks
```

```bash
benchmark report --api-target glm-fireworks --self-hosted-target glm-self-hosted
```

```bash
benchmark break-even --api-target glm-fireworks --self-hosted-target glm-self-hosted
```

```bash
benchmark show <run-id> --artifacts     # inspect one run in detail
benchmark export --output runs.json     # raw records
benchmark prune                         # clean up workspaces from crashed runs
```

Add `--mock` to `run` to exercise the whole pipeline with no model calls.

---

## The benchmark suite

Ten tasks across nine categories, on two fixture repositories
(`fixtures/payment-service`, `fixtures/devops-stack`):

| Task | Category | Difficulty |
|---|---|---|
| `payments-bug-001` | bug fix | easy |
| `payments-bug-002` | bug fix | medium |
| `payments-feature-001` | feature | medium |
| `payments-refactor-001` | refactor | medium |
| `payments-understanding-001` | repo understanding | medium |
| `payments-debug-001` | debugging | medium |
| `devops-001` | terminal / DevOps | medium |
| `payments-mcp-001` | MCP / external tools | hard |
| `payments-multistep-001` | multi-step agentic | hard |
| `payments-largectx-001` | large context | hard |

Suites: `smoke` (2 fast tasks), `backend` (6), `agentic` (4 hardest), `full` (all 10).

Fixtures must be git repositories (runs start from an exact commit, and task-specific
starting states live on branches), but a nested git repo cannot be committed into the outer
one. They are therefore distributed as **git bundles** in `fixtures/bundles/` and unpacked
by `benchmark init`. After editing a fixture, commit inside it and run
`benchmark fixtures --refresh` to regenerate its bundle.

Every task is graded by objective validation commands plus **hidden tests** that are copied
in only after the agent finishes. Each hidden suite has been verified to *fail* on the
unfixed repository and *pass* on a reference solution — so the tasks genuinely discriminate
rather than rewarding plausible-looking diffs.

The MCP task is backed by a **bundled local MCP server** (`mcp_servers/spec_server.py`)
serving a fixed Jira/Confluence corpus over stdio. No network, no credentials, fully
reproducible — and the rules it serves deliberately contradict the local codebase, so an
agent that skips the tools cannot pass.

---

## Documentation

| | |
|---|---|
| [`docs/benchmark-design.md`](docs/benchmark-design.md) | research question, hypotheses, methodology, architecture, scoring, limitations |
| [`docs/claude-code-model-replacement.md`](docs/claude-code-model-replacement.md) | what actually changes when you swap the model — verified vs. unknown |
| [`docs/methodology.md`](docs/methodology.md) | experimental controls and how to read results honestly |
| [`docs/metrics.md`](docs/metrics.md) | every metric, how it is derived, and what it does not mean |
| [`docs/pricing.md`](docs/pricing.md) | the four pricing models and the cost engine |
| [`docs/adding-models.md`](docs/adding-models.md) | add a model |
| [`docs/adding-providers.md`](docs/adding-providers.md) | add a provider or deployment |
| [`docs/creating-benchmark-tasks.md`](docs/creating-benchmark-tasks.md) | write a task, including hidden tests |
| [`docs/limitations.md`](docs/limitations.md) | what this benchmark cannot tell you |
| [`docs/threat-model.md`](docs/threat-model.md) | safety model for running an LLM with a shell |

A rendered example is in [`reports/sample-report.html`](reports/sample-report.html).

---

## Development

```bash
uv run pytest
```

```bash
uv run ruff check src tests
```

The framework's own test suite uses a mock agent runner and fixture repositories, so it is
fast, deterministic, and never spends money or touches a network.

Requires Python 3.12+, `git`, and the `claude` CLI on PATH.
