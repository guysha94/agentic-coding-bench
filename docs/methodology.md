# Methodology

How to run this benchmark so the numbers mean something — and how to read them without
over-claiming.

---

## 1. The controlled variable

The **only** intended difference between two runs of the same task is the target:

```
target = model × provider × deployment × pricing model
```

Held constant by construction:

| Constant | How |
|---|---|
| Repository and starting state | Fresh `git worktree` at an exact commit, verified clean |
| Task instructions | Byte-identical prompt; context files read from the workspace |
| MCP configuration | `--strict-mcp-config` + an explicit config file (empty when the task declares none) |
| Available tools | Explicit `--tools` allowlist, recorded per run |
| Environment | Allowlist, not the operator's shell; only the target's credential is injected |
| Settings / skills / plugins | `--setting-sources ""`, `--disable-slash-commands`, scratch `CLAUDE_CONFIG_DIR` |
| Timeout | From the task file, identical across targets |
| Session state | Fresh session, no `--resume`/`--continue`, `--no-session-persistence` |
| Validation | Identical commands, run at baseline and after |

If you disable config-dir isolation (needed for subscription auth), the run records that
fact and comparisons involving it are weaker. Say so when you report them.

## 2. Run protocol

1. Freeze the run spec (task + target + pricing snapshot).
2. Create the isolated workspace at the base commit; assert it is clean.
3. Record the environment fingerprint.
4. **Baseline validation** — so pre-existing failures are not counted as regressions.
5. Execute `claude -p`, streaming NDJSON to disk.
6. On timeout: SIGTERM the process group, then SIGKILL; keep partial artifacts.
7. Capture the git diff and changed-file list.
8. **Post validation** — visible checks, then hidden checks copied in fresh.
9. Normalise usage → compute cost → score.
10. Persist the run and destroy the workspace.

## 3. Repetition

Model behaviour is stochastic. **One run is evidence of almost nothing.**

```bash
benchmark run --suite backend --targets a,b,c --runs 5
```

Every `(task, target)` cell is run N times, stored independently, never overwritten.
Reports show mean, median, stdev, p50/p90 and a Wilson confidence interval.

Guidance: `--runs 3` to detect large differences, `--runs 5+` before making a purchasing
decision. Below 3, report raw runs rather than rates.

## 4. Ordering

Runs execute **target-major within a repetition round** — round 1 covers all targets and
tasks, then round 2, and so on — rather than finishing one target before starting the next.
This spreads provider-side drift (rate limits, capacity, a silent model update) roughly
evenly across targets instead of concentrating it in whichever ran last.

## 5. Reading the results honestly

**Do:**

- Read `cost_per_successful_task`, not cost per token.
- Read confidence intervals. Overlapping intervals mean the difference is unresolved.
- Read per-category results. The interesting finding is usually "target X is fine for
  simple work and falls apart on multi-step work", not a single ranking.
- Read `failed_calls_per_run` and `repeated_reads_per_run` before concluding a model
  "can't code" — it may be failing at *driving the tools*, which is a different problem
  with different fixes.
- Check `cost.incomplete` and `usage.complete`. An incomplete cost is a lower bound.

**Do not:**

- Compare runs across machines without noting it; wall clock is machine- and load-dependent.
- Compare a subscription target against API targets on cost without stating the allocation
  assumption.
- Generalise from this benchmark to "model X is better than model Y". The result is about
  *model X inside Claude Code on these tasks* — which is the decision we care about, and a
  narrower claim than it looks.
- Treat a proxy's failures as the model's failures. Check the transport-sensitive signals
  (`failed_calls` with malformed input, missing cache fields, non-`end_turn` stop reasons)
  first.

## 6. Threats to validity

| Threat | Mitigation |
|---|---|
| Ambient developer config leaking in | Hermetic config dir, verified to strip 258 tools → the declared set |
| Pre-existing test failures counted as regressions | Baseline validation phase |
| Agent gaming visible tests | Hidden tests, delivered only after the agent finishes |
| Overfitting to `expected_files` | Expected files are advisory; never a success gate |
| Provider drift over a long suite | Target-major ordering; `rate_limit_event`s recorded |
| Prices changing after the fact | Per-run pricing snapshot; reports use it by default |
| Normalisation bugs corrupting all cost | Raw usage preserved; normalisation is versioned |
| Small-N over-reading | Wilson intervals; explicit report warnings below 3 runs |
| A proxy being blamed on the model | Transport-sensitive signals reported separately |

## 7. Extending the suite toward 30–50 tasks

The proof-of-concept is 10 tasks. To scale:

1. **Add fixtures, not just tasks.** Two repositories is the current ceiling on realism.
   Add a larger repository (50k+ LOC) for genuine large-context work, and a non-Python one
   (TypeScript/Go) so results are not language-bound.
2. **Add tasks in matched pairs** — one easy, one hard, per category — so difficulty and
   category can be separated in analysis.
3. **Mine real history.** The highest-fidelity tasks come from real fixed bugs: check out
   the parent commit, use the real bug report as the prompt, and use the real test as the
   hidden test. This is the SWE-bench construction, and it scales.
4. **Keep every task discriminating.** Before shipping one, verify its hidden tests fail on
   the unfixed repository and pass on a reference solution. A task that everything passes
   measures nothing.
5. **Budget the runtime.** 40 tasks × 4 targets × 5 runs = 800 runs. At ~5 minutes each
   that is ~65 hours serially. Parallel execution is safe by construction (independent
   worktrees, config dirs and DB rows) but distorts wall-clock timing — run timing-sensitive
   suites serially, or drop the time weight when running in parallel.
