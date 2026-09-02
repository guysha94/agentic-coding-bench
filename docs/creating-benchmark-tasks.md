# Creating benchmark tasks

A good task has one property above all others: **it distinguishes a correct solution from a
plausible-looking one.** If every model passes, or none do, the task measures nothing.

---

## Anatomy

```yaml
id: payments-bug-002              # stable, unique; referenced by suites and the CLI
name: Fix duplicate charging on retried payments
category: bug_fix                 # see the category list below
difficulty: medium                # easy | medium | hard
version: 1                        # bump when the task's meaning changes

repository:
  path: ./fixtures/payment-service
  base_commit: bug/idempotency    # a SHA, tag, or branch name -- resolved and recorded
  isolation: worktree             # worktree | clone_temp
  setup_commands: []              # run in the fresh workspace before the agent starts

task:
  prompt: |
    Users can occasionally be charged twice when the same payment request is retried.
    ...
  context_files:                  # read from the WORKSPACE and appended to the prompt
    - CRASH_REPORT.txt

timeout_minutes: 20

validation:
  - name: unit-tests
    kind: test                    # test | build | lint | typecheck | custom
    command: $ACB_PYTHON -m pytest
  - name: lint
    kind: lint
    command: $ACB_PYTHON -m ruff check . || true
  - name: hidden-idempotency-tests
    kind: test
    hidden: true                  # copied in only AFTER the agent finishes
    command: $ACB_PYTHON -m pytest tests/hidden_test_idempotency.py
    provides_files:
      tests/hidden_test_idempotency.py: hidden/payments-bug-002/hidden_test_idempotency.py

success:
  require_tests_pass: true
  require_hidden_tests_pass: true
  require_no_regressions: true
  require_clean_build: true
  require_lint_pass: false
  require_typecheck_pass: false

# Optional metadata
language: python
framework: pytest
tool_call_budget: 25              # reference count for the efficiency score
expected_files: [...]             # ADVISORY ONLY -- never a success gate
expected_solution: >              # prose, for human reviewers
  ...
requires_network: false
tags: [python, backend, idempotency]
```

## Categories

`bug_fix`, `feature`, `refactor`, `repo_understanding`, `large_context`, `debugging`,
`devops`, `mcp`, `multi_step`. New categories work without a code change; they just sort
last in reports.

---

## Rules that matter

### Use `$ACB_PYTHON`, not a bare `python`

The harness injects `ACB_PYTHON` pointing at the interpreter it is running under (which has
`pytest` and `ruff`), and prepends that interpreter's `bin` directory to `PATH`. A bare
`python` resolves to whatever is first on the operator's `PATH` — which is how a task
silently "fails" on one machine and passes on another.

Also: don't add `-q` if the project's `addopts` already has it. Two `-q` flags make `-qq`,
which suppresses the summary line the harness parses for per-test counts.

### Never gate on file names

Different correct solutions touch different files. `expected_files` is reported as a hint
and is **never** used to decide success. Gate on behaviour.

### Hidden tests are the real grader

Visible tests tell the agent what to aim at. Hidden tests check whether it actually got
there. Target the parts that are easy to get *almost* right:

- exact status codes, action strings, and message subjects
- side effects that must **also** be suppressed (no duplicate audit entry, no second
  notification, no consumed rate-limit headroom)
- boundaries the visible tests don't cover
- the shortcut that silences the symptom without fixing the cause — e.g. for a
  naive/aware datetime crash, assert that live timestamps *keep* their timezone, so
  stripping `tzinfo` everywhere fails

Name hidden files `hidden_test_*.py`. That does **not** match pytest's default collection
patterns, so a stray copy left in a workspace cannot be picked up by a bare `pytest` run —
while the validation command, which names the file explicitly, still collects it.

### Verify the task discriminates — before shipping it

This is not optional. For each new task:

```bash
git -C fixtures/<repo> worktree add --detach /tmp/check <base_commit>
cp tasks/hidden/<task-id>/hidden_test_*.py /tmp/check/tests/
cd /tmp/check && $ACB_PYTHON -m pytest tests/hidden_test_*.py     # MUST fail
# apply a reference solution
cd /tmp/check && $ACB_PYTHON -m pytest                            # MUST pass
git -C fixtures/<repo> worktree remove --force /tmp/check
```

Every task in this repository has been verified this way.

### Set a realistic `tool_call_budget`

Solve the task yourself (or watch one good run) and use roughly that count. It only affects
the efficiency dimension, but a wildly wrong budget makes that dimension noise.

### Write the prompt as a stimulus, not a spec dump

The prompt is the experimental stimulus and must be byte-identical across targets. Aim for
what a competent colleague would actually be told:

- **Bug fix / debugging:** describe the *symptom*, not the fix. Attach the real artifact
  (log, stack trace) via `context_files`, committed into the fixture so every workspace has it.
- **Feature:** be precise about observable behaviour, since hidden tests will check it
  exactly. Ambiguity here measures guessing, not ability.
- **Repo understanding:** ask for the outcome and let the agent find the right place. Say
  "put it in the layer that owns rules of this kind", not "edit `payment_service.py`".
- **Refactor:** state the invariant — "externally observable behaviour must not change".

---

## Fixture repositories

Fixtures must be git repositories (runs start from an exact commit) and must be **clean**
before a suite starts — `doctor` checks this.

Use **branches for task-specific starting states**. A linear history forces later tasks to
inherit earlier bugs; branches keep each starting state independent:

```
main                  healthy baseline
bug/idempotency       main + a failing test documenting the bug
bug/fee-rounding      main + a rounding regression
bug/history-crash     main + a crashing legacy importer + the on-call report
```

A task then sets `base_commit: bug/idempotency`. Refs are resolved to full SHAs and
recorded per run, so this stays reproducible.

---

## MCP tasks

Point the task at an MCP config and declare what a correct solution should use:

```yaml
mcp:
  config_file: ./config/mcp/spec.json
  required_servers: [specs]
  expected_tools:
    - mcp__specs__get_jira_issue
    - mcp__specs__get_confluence_page
  forbidden_tools: []
  max_calls: 8
```

**Design the task so external retrieval is unavoidable.** The bundled
`mcp_servers/spec_server.py` serves a Confluence page whose rules deliberately *contradict*
the local codebase — the processing fee is not refunded, and a partial refund leaves the
status unchanged. An agent that guesses from the code fails the hidden tests. That is what
makes it a real MCP test rather than a coding test with extra steps.

Prefer a local stdio MCP server over a live service: a live Jira makes the task depend on
network conditions, credentials and mutable content. If you must use a live service, set
`requires_network: true` so those tasks can be excluded from headline numbers.

---

## Adding the task to a suite

```yaml
# config/suites/backend.yaml
id: backend
name: Backend suite
tasks:
  - payments-bug-001
  - my-new-task
```

Then:

```bash
benchmark validate
benchmark run --task my-new-task --target <target> --runs 1
benchmark show <run-id> --artifacts
```

Read `git.diff` and `tool_calls.jsonl` from that first run. If the agent misunderstood the
prompt, fix the prompt — not the grader.
