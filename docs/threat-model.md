# Threat model

This framework hands a language model a shell and asks it to change code. That is the thing
being measured, so it cannot be prevented — it has to be **contained**.

---

## Assets

| Asset | Why it matters |
|---|---|
| The operator's source repositories | An agent writing outside its workspace could damage real work |
| Developer credentials in the operator's shell | Cloud keys, GitHub tokens, database URLs, SSH agent access |
| Production systems reachable from the machine | An agent could act on them by accident or by injection |
| Provider API credentials | Financial exposure |
| Benchmark artifacts | Persisted logs could carry secrets into version control |
| Result integrity | A contaminated run produces a wrong business decision |

## Adversaries and failure modes

| # | Threat | Realistic? |
|---|---|---|
| T1 | Agent writes outside the workspace (bad path, `cd ..`, absolute path) | **High** — ordinary model error |
| T2 | Agent runs a destructive command inside the workspace | **High** — and acceptable; the workspace is disposable |
| T3 | Agent reads credentials from the environment and sends them somewhere | Medium |
| T4 | Secrets land in persisted logs and get committed | **High** without redaction |
| T5 | Prompt injection from fixture or MCP content redirects the agent | Medium — MCP content is attacker-controllable in a real deployment |
| T6 | Agent reaches production because the machine has network access to it | Medium |
| T7 | A crashed run leaves worktrees or processes behind | High, low impact |
| T8 | Benchmark results are contaminated by ambient config | **High** without config isolation |
| T9 | A malicious or compromised proxy endpoint exfiltrates prompts | Low likelihood, high impact |

---

## Controls

### Workspace confinement (T1, T2)

- Every run executes with `cwd` set to a throwaway `git worktree`, never the source repo.
- The worktree is created from an exact commit, verified clean, and destroyed afterwards.
- The source repository is never modified — asserted by a test that checks
  `git status --porcelain` is empty after a run that edits and adds files.
- `provides_files` destinations are path-checked and refuse to escape the workspace.

The agent *is* deliberately allowed to run arbitrary commands **inside** the workspace.
That is the capability under test. The mitigation is that the workspace is disposable and
the environment around it is minimal.

### Environment minimisation (T3, T6)

- The agent process starts from an **allowlist** (`PATH`, `HOME`, locale, toolchain roots),
  not the operator's shell. Every `*_TOKEN`, `*_KEY`, `AWS_*`, and `DATABASE_URL` is
  dropped — verified by test.
- Only the target's declared credential is injected, under the variable the adapter
  expects.
- Benchmark environment variables are namespaced `ACB_*` and separate from developer ones.

### Secret redaction (T4)

- Values of variables whose names match `API_KEY|AUTH_TOKEN|SECRET|PASSWORD|CREDENTIAL|
  PRIVATE_KEY|SESSION_TOKEN` are redacted from every persisted artifact.
- Structural patterns are redacted even when the value was never in our environment:
  `sk-ant-*`, `sk-*`, `ghp_*`/`gho_*`, `AKIA*`, JWTs, and `Bearer <token>`.
- Redaction covers `agent.log`, `stdout.log`, `stderr.log`, `git.diff`,
  `usage/provider_raw.json`, and validation output tails.

Redaction is best-effort pattern matching, not a guarantee. **Review artifacts before
publishing them**, and prefer a dedicated benchmark credential with a low spend cap.

### Configuration isolation (T8)

- Scratch `CLAUDE_CONFIG_DIR`, `--setting-sources ""`, `--disable-slash-commands`,
  `--strict-mcp-config`, explicit `--tools`.
- Verified to reduce a session from the operator's ambient 258 tools / 20 MCP servers / 234
  slash commands to exactly the declared set.
- When isolation is disabled (required for subscription auth), the run records it and the
  reproducibility caveat travels with the result.

### Injection containment (T5)

- Fixture content is authored in this repository and reviewed.
- The bundled MCP server serves a fixed local corpus — no attacker-controlled input.
- MCP servers are pinned per task by `--strict-mcp-config`; an agent cannot add one.
- Live-service MCP tasks must be marked `requires_network: true`, and their content should
  be treated as untrusted. **Never point a benchmark task at an MCP server whose content an
  outside party can edit.**

### Process hygiene (T7)

- The agent runs in its own process group; a timeout sends `SIGTERM` then `SIGKILL` to the
  whole group, so test runners and dev servers it spawned die with it.
- Cleanup is idempotent and runs in a `finally` block, so artifacts survive a harness crash.
- `benchmark prune` removes worktrees left by a killed process.
- Auto-update is disabled (`DISABLE_AUTOUPDATER=1`) so the CLI version cannot change
  mid-suite.

### Endpoint trust (T9)

Prompts contain your source code. An `endpoint:` in a target receives all of it. Point
targets only at endpoints you control or have a contract with, and prefer network-level
egress restrictions over trusting configuration.

---

## Residual risk — accepted

1. **The agent can destroy its own workspace.** By design.
2. **The agent can reach the network.** Needed for the model API itself; a task could in
   principle fetch something. Run on an isolated network, or in a container, when that
   matters.
3. **Redaction is heuristic.** A novel secret format could pass through.
4. **Worktrees share the fixture's object store.** A sufficiently determined `git` command
   inside a worktree could reach it. Use `isolation: clone_temp` for untrusted tasks.
5. **No container isolation by default.** Worktrees are fast and adequate for
   repository-authored tasks; they are not a security boundary against hostile code.
   `isolation: container` is reserved and not implemented.

## Operating recommendations

- Run benchmarks on a dedicated machine or VM, not a developer laptop with production
  access.
- Use a dedicated benchmark API credential with a hard spend cap.
- Run `benchmark doctor` before every suite: it catches a dirty fixture, a missing
  credential, and incomplete pricing before you spend anything.
- Review `git.diff` and the artifact directory before publishing results.
- Never place production credentials in `config/targets.yaml`; reference them by env var
  name only — the value is never written to config or artifacts.
