# Pricing and the cost engine

Two rules govern everything here:

1. **No price is ever hardcoded in source.** Every rate comes from `config/targets.yaml`.
2. **`null` means unknown, not free.** A missing rate marks the cost `incomplete` and the
   reported figure is a lower bound — a configuration gap can never make a target look
   cheap.

---

## Why we don't use Claude Code's own cost number

`claude -p` reports `total_cost_usd` and per-model `costUSD`. Observed on a real run:

```json
"modelUsage": { "claude-sonnet-5": {
    "costUSD": 0.7524, "provider": "firstParty", "costBasis": "list" } }
```

That is computed from Claude Code's **own** first-party price table. For a proxied or
self-hosted model it prices foreign tokens at Anthropic list rates, or reports `0`.

So the framework recomputes every cost from raw token counts. `total_cost_usd` is stored in
`usage/provider_raw.json` as a cross-check and never used for comparison.

---

## Normalized usage

Providers disagree about whether cached tokens sit *inside* or *beside* the input count.
Getting this wrong silently corrupts every cost number, so normalisation is explicit,
versioned, and owned by the adapter.

```python
NormalizedUsage(
    input_tokens,          # billable, NON-cached prompt tokens
    output_tokens,         # includes reasoning tokens
    cached_input_tokens,   # cache reads
    cache_write_tokens,    # cache creation
    reasoning_tokens,      # a SUBSET of output_tokens
    total_turns,
)
```

| Dialect | Provider convention | Normalisation |
|---|---|---|
| `anthropic_v1` | `input_tokens` **excludes** cache read and creation | `input = input_tokens` (pass-through) |
| `openai_compat_v1` | `prompt_tokens` **includes** `prompt_tokens_details.cached_tokens` | `input = prompt_tokens − cached_tokens`, clamped at 0 |

Both dialects yield the same `total_billable_input` for the same underlying request — this
is asserted in the test suite so it cannot silently regress.

`reasoning_tokens` are treated as already inside `output_tokens`. They are only priced
separately when the target sets `reasoning_per_million_tokens`, in which case they are
subtracted from output first. No token is ever billed twice.

Both the normalised and raw payloads are written per run, so normalisation can be corrected
retroactively without re-running anything.

---

## 1. Token pricing (API)

```yaml
pricing:
  type: token
  currency: USD
  effective_date: "2026-01-15"     # when you copied these rates
  source: "https://provider.example/pricing"
  input_per_million_tokens: 3.00
  output_per_million_tokens: 15.00
  cached_input_per_million_tokens: 0.30
  cache_write_per_million_tokens: 3.75
  reasoning_per_million_tokens: null    # only if billed SEPARATELY from output
  extra_token_classes:                  # provider-specific billable counters
    web_search_requests: 0.01
```

```
input_cost         = input_tokens         / 1e6 × input_per_million
output_cost        = output_tokens        / 1e6 × output_per_million
cached_input_cost  = cached_input_tokens  / 1e6 × cached_input_per_million
cache_write_cost   = cache_write_tokens   / 1e6 × cache_write_per_million
reasoning_cost     = reasoning_tokens     / 1e6 × reasoning_per_million

total_model_cost   = sum of the above + extra provider-specific usage cost
```

**Cache savings** (`input_price − cached_price`, over cached tokens) are reported only when
both rates are known — we never imply a saving we cannot substantiate.

---

## 2. Infrastructure pricing (self-hosted)

```yaml
pricing:
  type: infrastructure
  currency: USD
  gpu:
    type: B200
    count: 4
    hourly_cost_per_gpu: 4.50
  additional_machine_hourly_cost: 1.20
  utilization_assumption: 0.40
  # optional, applied when set:
  minimum_billing_seconds: 60
  discount_multiplier: 0.4        # e.g. a 60% spot discount
  storage_hourly_cost: 0.10
  network_cost_per_run: 0.00
```

Two figures, **never summed**:

```
marginal_run_cost = runtime_hours × (gpu_count × gpu_hourly + machine_hourly + storage)
                    × discount_multiplier + network_cost_per_run

effective_cost    = marginal_run_cost / utilization_assumption
```

*Marginal* is the infrastructure time this run directly consumed. *Effective* is what that
capacity really costs given that an idle GPU still bills. The headline is the effective
figure when a utilisation is configured; otherwise it is marginal, and the report warns
that this understates dedicated capacity.

A flat `total_hourly_cost:` may be used instead of the `gpu:` breakdown.

---

## 3. Subscription pricing

```yaml
pricing:
  type: subscription
  currency: USD
  monthly_cost: 200.00
  allocation_strategy: active_user   # active_user | observed_usage_share | working_hours | manual
  seats: 1
  tasks_per_month: 200               # active_user
  working_hours_per_month: 160       # working_hours
  manual_cost_per_run: 0.50          # manual
```

| Strategy | Per-run cost |
|---|---|
| `active_user` | `(monthly / seats) / tasks_per_month` |
| `working_hours` | `(monthly / seats) / working_hours_per_month × runtime_hours` |
| `observed_usage_share` | `(monthly / seats) / runs observed in this batch` |
| `manual` | `manual_cost_per_run` |

Subscription economics are genuinely ambiguous, so the framework refuses to pretend there
is one correct answer. Reports show **both** the raw monthly cost and the allocated
per-run figure, always labelled an estimate with its strategy named, and marked with `*`
in tables.

---

## 4. Unknown pricing

```yaml
pricing:
  type: unknown
  note: "pricing not yet negotiated"
```

`total_cost` is `null` and the target is excluded from cost aggregates — but still competes
on quality. This is the honest default for a provider whose pricing is not settled.

---

## Historical reproducibility

Every run persists `pricing_snapshot.json`:

```json
{
  "target": "glm-fireworks",
  "provider": "fireworks",
  "model": "...",
  "pricing_type": "token",
  "currency": "USD",
  "pricing_effective_date": "2026-01-15",
  "pricing_snapshot": { "input_per_million_tokens": 0.5, "...": null },
  "normalization_version": "1.0.0"
}
```

Reports use the snapshot from the run **by default**, so a price change next quarter does
not silently rewrite last quarter's conclusions. To ask the what-if:

```bash
benchmark report --reprice-current
```

which recomputes historical usage at today's configured rates and labels the output
accordingly.

---

## Break-even analysis

```bash
benchmark break-even --api-target glm-fireworks --self-hosted-target glm-self-hosted
```

Given measured API cost per successful task and measured self-hosted throughput
(successful tasks per wall-clock hour), self-hosting wins when

```
daily_tasks × api_cost_per_task  ≥  24h × hourly_infra_cost / utilisation
```

The report prints cost per successful task and break-even volume (tasks/day and
tokens/day) at each utilisation scenario, with an explicit conclusion and caveats.

**It is an estimate.** Benchmark wall clock includes agent thinking, tool execution and
test runs, so observed throughput understates raw GPU throughput; concurrency is not
modelled, which moves the break-even point down. And cost says nothing about quality —
check success rates before acting on it.
