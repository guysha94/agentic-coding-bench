# Metrics

Every metric below, how it is derived, and — just as importantly — what it does **not**
mean. All of them come from the `stream-json` trajectory, validation exit codes, or the
git diff; none are guessed.

---

## Outcome metrics

| Metric | Derivation | Caveat |
|---|---|---|
| `success` | All of the task's `success:` gates pass. | Binary by design. A run that fixed 90% of a task scores 0 here — read `test_pass_rate` alongside it. |
| `test_pass_rate` | Parsed per-test counts when the runner's output is recognisable (pytest / jest / go), else the fraction of passing test *commands*. | A task with one big test command has a coarse rate. |
| `hidden_test_pass_rate` | Same, over checks marked `hidden: true`. | Hidden tests are copied in only after the agent finishes. |
| `build_passed` / `lint_passed` / `typecheck_passed` | All checks of that kind exited 0. | `None` when the task configures no such check — scoring drops the dimension rather than assuming a pass. |
| `regression_count` | Checks that **passed at baseline and fail after** the agent ran. | This is why validation runs twice. A check that was already red is not the agent's fault. |
| `files_changed` / `lines_added` / `lines_removed` | `git diff --numstat` against the base commit, with untracked files staged first. | Size is not quality; a large diff is not automatically worse. |
| `touched_expected_files` | Overlap with `expected_files`. | **Advisory only, never a gate.** Different correct solutions touch different files. |

## Time metrics

| Metric | Derivation | Caveat |
|---|---|---|
| `wall_clock_ms` | The CLI's own `result.duration_ms`, falling back to our measured subprocess time (and always ours on timeout). | Includes provider queueing, which is outside our control. |
| `api_ms` | `result.duration_api_ms`. | |
| `ttft_ms` | `result.ttft_ms`. | Sensitive to prompt-cache state. |
| `time_to_first_edit_ms` | Timestamp of the first **successful** mutating call (`Edit`/`Write`/`NotebookEdit`, or a `Bash` command matching a mutation pattern) minus session start. | A failed edit does not count. A model that plans longer before editing is not necessarily worse. |
| `time_to_first_passing_validation_ms` | First post-phase check that passed. | Only meaningful for tasks with staged checks. |
| `timeout` | The run hit the task's `timeout_minutes`. | Counted separately from `failure`: timing out and being wrong are different problems. |

## Agent tool metrics

Derived from `tool_use` blocks in `assistant` events paired with `tool_result` blocks in
`user` events.

| Metric | Derivation |
|---|---|
| `total_calls` | Every tool invocation. |
| `bash_calls` / `read_calls` / `write_calls` / `edit_calls` / `search_calls` / `git_calls` / `mcp_calls` / `web_calls` / `subagent_calls` | Tool name mapped to a family. A `Bash` call whose command starts with `git` counts as both `git` and `bash`. |
| `failed_calls` | `tool_result.is_error == true`. Includes permission denials and malformed input. |
| `repeated_calls` | Same tool with a byte-identical normalised input, seen before in this run. |
| `repeated_reads` | Reads of a path already read. **The clearest single signal of context loss.** |
| `distinct_files_read` / `distinct_files_modified` | Unique paths. |
| `sequence` | The full ordered list, persisted to `tool_calls.jsonl` with timestamps and rendered as `Search → Read → Read → Bash → Edit`. |

**MCP-specific:** `mcp_servers_used`, `mcp_expected_tools_used`, `mcp_missing_tools`,
`mcp_forbidden_tools_used`, `mcp_redundant_calls` (repeats, plus anything above the task's
`max_calls`).

> A high tool count is not automatically bad — thorough investigation is legitimate. Read
> it against `failed_calls` and `repeated_reads`, which distinguish *thorough* from *lost*.

## Model usage metrics

Normalised per `docs/pricing.md`. Recorded as both `usage/normalized.json` and
`usage/provider_raw.json`.

| Metric | Meaning |
|---|---|
| `input_tokens` | Billable, **non-cached** prompt tokens. |
| `cached_input_tokens` | Cache reads (discounted). |
| `cache_write_tokens` | Cache creation (often a premium). |
| `output_tokens` | Completion tokens, **including** reasoning tokens. |
| `reasoning_tokens` | Thinking tokens — a *subset* of output, priced separately only when the target configures a distinct rate. |
| `total_turns` | Assistant messages. |
| `complete` | `false` when the provider omitted fields we expected. Propagates to `cost.incomplete`. |

## Efficiency metrics (derived)

Computed per target during aggregation:

- `cost_per_successful_task` = total cost ÷ successful runs — **the headline number**
- `tool_calls_per_successful_task`, `wall_time_per_successful_task_s`,
  `tokens_per_successful_task`, `mcp_calls_per_successful_task`
- `failed_calls_per_run`, `repeated_reads_per_run`
- `cost_per_failed_task` — what unsuccessful attempts cost you

`cost_per_successful_task` is deliberately *total* cost over *successful* runs: failures
still consume tokens and wall time. A model that is 10× cheaper per run but succeeds a
fifth as often is more expensive in practice, and this metric shows it.

## Statistics

Per target, over repeated runs: mean, median, standard deviation, p50, p90, min, max, and
success rate with a **95% Wilson score interval**. Wilson rather than a naive proportion
because at N=3–5 a naive interval is badly wrong at the extremes — 4/4 successes is
reported as 51–100%, not 100%.

## Metrics we deliberately do not compute

- **Subjective code quality.** No LLM-judge "looks good" score. Only lint, type-check and
  tests, which are reproducible.
- **Per-tool latency attribution to the model.** Tool execution time is dominated by the
  command, not the model.
- **Token cost from the CLI's own `total_cost_usd`.** First-party price table only; kept as
  a cross-check, never used for comparison.
