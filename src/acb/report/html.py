"""HTML / JSON / CSV report generation.

The report answers the executive questions from the brief: overall winner, reliability,
speed, cost per successful task, agent efficiency, MCP performance, per-category
breakdown, and cost-vs-quality. Every composite score is shown next to the raw metrics
behind it, because a single number hides exactly the trade-offs this project exists to
surface.
"""

from __future__ import annotations

import csv
import json
from io import StringIO
from pathlib import Path

from jinja2 import Environment, select_autoescape

from acb.report.aggregate import Comparison
from acb.report.breakeven import BreakEvenAnalysis

_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agentic Coding Benchmark Report</title>
<style>
  :root {
    --bg:#fbfbfa; --panel:#fff; --ink:#1a1a19; --muted:#6b6b68; --line:#e6e5e1;
    --accent:#b5563a; --ok:#2f7d4f; --warn:#a8791c; --bad:#a83232;
  }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#191917; --panel:#211f1d; --ink:#f0eee9; --muted:#a4a099; --line:#33302c;
            --accent:#d97757; --ok:#63b37f; --warn:#d4a34a; --bad:#d96a6a; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Helvetica,Arial,sans-serif; }
  .wrap { max-width:1180px; margin:0 auto; padding:2.5rem 1.25rem 5rem; }
  h1 { font-size:1.9rem; margin:0 0 .3rem; letter-spacing:-.02em; }
  h2 { font-size:1.15rem; margin:2.6rem 0 .8rem; letter-spacing:-.01em; }
  .sub { color:var(--muted); font-size:.9rem; margin-bottom:2rem; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:10px;
           padding:1.1rem 1.25rem; margin-bottom:1rem; }
  .scroll { overflow-x:auto; }
  table { border-collapse:collapse; width:100%; font-size:.88rem; min-width:640px; }
  th,td { text-align:right; padding:.5rem .65rem; border-bottom:1px solid var(--line);
          white-space:nowrap; }
  th:first-child, td:first-child { text-align:left; position:sticky; left:0;
          background:var(--panel); }
  th { font-weight:600; color:var(--muted); font-size:.78rem; text-transform:uppercase;
       letter-spacing:.04em; }
  tbody tr:hover td { background:rgba(127,127,127,.06); }
  .ok{color:var(--ok);} .warn{color:var(--warn);} .bad{color:var(--bad);}
  .muted{color:var(--muted);} .mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
  .bar { display:inline-block; height:8px; background:var(--accent); border-radius:4px;
         vertical-align:middle; margin-right:.4rem; }
  .cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:.75rem; }
  .card .k { color:var(--muted); font-size:.75rem; text-transform:uppercase;
             letter-spacing:.05em; }
  .card .v { font-size:1.45rem; font-weight:600; margin-top:.15rem; letter-spacing:-.02em; }
  .note { border-left:3px solid var(--warn); padding:.5rem .85rem; margin:.5rem 0;
          color:var(--muted); font-size:.85rem; background:rgba(168,121,28,.07);
          border-radius:0 6px 6px 0; }
  .scatter { width:100%; height:auto; }
  .legend { font-size:.8rem; color:var(--muted); margin-top:.4rem; }
  footer { margin-top:3rem; color:var(--muted); font-size:.8rem;
           border-top:1px solid var(--line); padding-top:1rem; }
</style></head><body><div class="wrap">

<h1>Agentic Coding Benchmark</h1>
<div class="sub">{{ comp.total_runs }} runs &middot; {{ comp.targets|length }} targets &middot;
  {{ comp.task_ids|length }} tasks &middot; generated {{ comp.generated_at }}</div>

<div class="cards">
  <div class="panel card"><div class="k">Best success rate</div>
    <div class="v">{{ best_quality_name }}</div>
    <div class="muted">{{ '%.0f'|format(best_quality_rate*100) }}% of tasks solved</div></div>
  <div class="panel card"><div class="k">Lowest cost / success</div>
    <div class="v">{{ best_value_name }}</div>
    <div class="muted">{{ best_value_cost }}</div></div>
  <div class="panel card"><div class="k">Fastest</div>
    <div class="v">{{ fastest_name }}</div>
    <div class="muted">{{ fastest_time }} median</div></div>
</div>

<h2>Overall</h2>
<div class="panel scroll"><table>
<thead><tr><th>Target</th><th>Model</th><th>Provider</th><th>Deploy</th><th>Runs</th>
<th>Success</th><th>95% CI</th><th>Score</th><th>Median time</th><th>Cost/success</th>
<th>Tool calls</th></tr></thead>
<tbody>
{% for t in targets %}
<tr>
  <td><strong>{{ t.target_id }}</strong></td>
  <td class="mono">{{ t.model }}</td><td>{{ t.provider }}</td><td>{{ t.deployment_type }}</td>
  <td>{{ t.runs }}</td>
  <td class="{{ 'ok' if t.success_rate >= 0.8 else ('warn' if t.success_rate >= 0.5 else 'bad') }}">
    <span class="bar" style="width:{{ (t.success_rate*46)|round(0,'floor')|int }}px"></span>
    {{ '%.0f'|format(t.success_rate*100) }}%</td>
  <td class="muted">{{ '%.0f'|format(t.success_rate_ci[0]*100) }}&ndash;{{ '%.0f'|format(t.success_rate_ci[1]*100) }}%</td>
  <td>{{ '%.3f'|format(t.score.mean or 0) }}</td>
  <td>{{ fmt_secs(t.duration.median) }}</td>
  <td>{{ fmt_money(t.cost_per_successful_task, t.currency) }}{% if t.cost_is_estimate %}<span class="warn" title="estimate">*</span>{% endif %}</td>
  <td>{{ '%.1f'|format(t.tool_calls.mean or 0) }}</td>
</tr>
{% endfor %}
</tbody></table>
<div class="legend">Cost per successful task = total cost / successful runs. This, not
token price, is the decision-grade number: a cheap target that fails often costs more.
<span class="warn">*</span> = estimate (subscription allocation or utilisation assumption).</div>
</div>

{% if warnings %}
{% for w in warnings %}<div class="note">{{ w }}</div>{% endfor %}
{% endif %}

<h2>Reliability, speed and efficiency</h2>
<div class="panel scroll"><table>
<thead><tr><th>Target</th><th>Solved</th><th>Failed</th><th>Timeout</th><th>Error</th>
<th>p50 time</th><th>p90 time</th><th>Turns</th><th>Failed calls/run</th>
<th>Repeat reads/run</th><th>Tools/success</th></tr></thead>
<tbody>
{% for t in targets %}
<tr><td><strong>{{ t.target_id }}</strong></td>
  <td class="ok">{{ t.successes }}</td><td>{{ t.failures }}</td>
  <td class="{{ 'bad' if t.timeouts else 'muted' }}">{{ t.timeouts }}</td>
  <td class="{{ 'bad' if t.errors else 'muted' }}">{{ t.errors }}</td>
  <td>{{ fmt_secs(t.duration.p50) }}</td><td>{{ fmt_secs(t.duration.p90) }}</td>
  <td>{{ '%.1f'|format(t.turns.mean or 0) }}</td>
  <td class="{{ 'bad' if t.failed_calls_per_run > 2 else '' }}">{{ '%.1f'|format(t.failed_calls_per_run) }}</td>
  <td>{{ '%.1f'|format(t.repeated_reads_per_run) }}</td>
  <td>{{ '%.1f'|format(t.tool_calls_per_successful_task or 0) }}</td>
</tr>
{% endfor %}
</tbody></table>
<div class="legend">Failed calls and repeated reads are the clearest signals of weak agentic
control: they distinguish "cannot write code" from "cannot drive the tools".</div></div>

<h2>Cost breakdown</h2>
<div class="panel scroll"><table>
<thead><tr><th>Target</th><th>Pricing</th><th>Total</th><th>Mean/run</th><th>Median/run</th>
<th>Cost/success</th><th>Cost/failure</th><th>Input tok</th><th>Output tok</th>
<th>Cached tok</th><th>Cache saving</th></tr></thead>
<tbody>
{% for t in targets %}
<tr><td><strong>{{ t.target_id }}</strong></td><td>{{ t.pricing_type }}</td>
  <td>{{ fmt_money(t.total_cost, t.currency) }}</td>
  <td>{{ fmt_money(t.cost.mean, t.currency) }}</td>
  <td>{{ fmt_money(t.cost.median, t.currency) }}</td>
  <td>{{ fmt_money(t.cost_per_successful_task, t.currency) }}</td>
  <td>{{ fmt_money(t.cost_per_failed_task, t.currency) }}</td>
  <td>{{ '{:,}'.format(t.input_tokens) }}</td>
  <td>{{ '{:,}'.format(t.output_tokens) }}</td>
  <td>{{ '{:,}'.format(t.cached_input_tokens) }}</td>
  <td class="ok">{{ fmt_money(t.cache_savings, t.currency) }}</td>
</tr>
{% endfor %}
</tbody></table>
<div class="legend">Costs use each run's own pricing snapshot, so historical runs are never
silently re-priced at today's rates.</div></div>

<h2>By task type</h2>
<div class="panel scroll"><table>
<thead><tr><th>Target</th>{% for c in comp.categories %}<th>{{ c }}</th>{% endfor %}</tr></thead>
<tbody>
{% for t in targets %}
<tr><td><strong>{{ t.target_id }}</strong></td>
{% for c in comp.categories %}
  {% set cat = t.by_category.get(c) %}
  {% if cat %}<td class="{{ 'ok' if cat.success_rate >= 0.8 else ('warn' if cat.success_rate >= 0.5 else 'bad') }}">
    {{ '%.0f'|format(cat.success_rate*100) }}% <span class="muted">({{ cat.successes }}/{{ cat.runs }})</span></td>
  {% else %}<td class="muted">&mdash;</td>{% endif %}
{% endfor %}
</tr>
{% endfor %}
</tbody></table></div>

{% if mcp_targets %}
<h2>MCP / external tool usage</h2>
<div class="panel scroll"><table>
<thead><tr><th>Target</th><th>MCP success</th><th>MCP calls/success</th><th>Redundant calls</th>
<th>Missing expected tools</th><th>Forbidden tools used</th></tr></thead>
<tbody>
{% for row in mcp_targets %}
<tr><td><strong>{{ row.target_id }}</strong></td>
  <td class="{{ 'ok' if row.success_rate >= 0.8 else ('warn' if row.success_rate >= 0.5 else 'bad') }}">{{ '%.0f'|format(row.success_rate*100) }}%</td>
  <td>{{ '%.1f'|format(row.calls_per_success) }}</td>
  <td class="{{ 'warn' if row.redundant else '' }}">{{ row.redundant }}</td>
  <td class="{{ 'bad' if row.missing else 'muted' }}">{{ row.missing }}</td>
  <td class="{{ 'bad' if row.forbidden else 'muted' }}">{{ row.forbidden }}</td>
</tr>
{% endfor %}
</tbody></table>
<div class="legend">Chose the right server and tool, without redundant calls &mdash; the
signals that separate genuine MCP competence from lucky guessing.</div></div>
{% endif %}

<h2>Cost vs quality</h2>
<div class="panel">
{% if scatter.points %}
<svg class="scatter" viewBox="0 0 720 360" role="img" aria-label="Cost versus success rate">
  <line x1="60" y1="310" x2="690" y2="310" stroke="var(--line)" stroke-width="1"/>
  <line x1="60" y1="30" x2="60" y2="310" stroke="var(--line)" stroke-width="1"/>
  {% for g in scatter.gridlines %}
  <line x1="60" y1="{{ g.y }}" x2="690" y2="{{ g.y }}" stroke="var(--line)"
        stroke-width="1" stroke-dasharray="3 4"/>
  <text x="52" y="{{ g.y + 4 }}" text-anchor="end" font-size="11"
        fill="var(--muted)">{{ g.label }}</text>
  {% endfor %}
  {% for p in scatter.points %}
  <circle cx="{{ p.x }}" cy="{{ p.y }}" r="7" fill="var(--accent)" fill-opacity=".8"/>
  <text x="{{ p.x + 11 }}" y="{{ p.y + 4 }}" font-size="11" fill="var(--ink)">{{ p.label }}</text>
  {% endfor %}
  <text x="375" y="345" text-anchor="middle" font-size="12" fill="var(--muted)">
    cost per successful task ({{ scatter.currency }}) &rarr;</text>
  <text x="18" y="170" text-anchor="middle" font-size="12" fill="var(--muted)"
        transform="rotate(-90 18 170)">success rate &rarr;</text>
</svg>
<div class="legend">Up and to the left is better: high success rate at low cost per success.</div>
{% else %}
<div class="muted">Not enough priced runs to plot cost vs quality.</div>
{% endif %}
</div>

{% if comp.recommendations %}
<h2>Routing recommendations</h2>
<div class="panel scroll"><table>
<thead><tr><th>Task category</th><th>Best quality</th><th>Success</th><th>Best value</th>
<th>Cost/success</th><th>Verdict</th></tr></thead>
<tbody>
{% for r in comp.recommendations %}
<tr><td><strong>{{ r.category }}</strong></td>
  <td>{{ r.best_quality_target }}</td>
  <td>{{ '%.0f'|format(r.best_quality_success_rate*100) }}%</td>
  <td>{{ r.get('best_value_target', '—') }}</td>
  <td>{{ fmt_money(r.get('best_value_cost_per_success'), 'USD') }}</td>
  <td class="{{ 'ok' if r.get('cheaper_and_as_good') else 'muted' }}">
    {% if r.get('cheaper_and_as_good') %}route here to save cost
    {% else %}keep the quality leader{% endif %}</td>
</tr>
{% endfor %}
</tbody></table>
<div class="legend">"Route here to save cost" fires only when the cheaper target's
confidence interval overlaps the leader's &mdash; i.e. the observed quality gap is not
statistically resolved at this sample size.</div></div>
{% endif %}

{% if breakeven %}
<h2>Break-even: API vs self-hosted</h2>
<div class="panel">
  <p><strong>{{ breakeven.api_target }}</strong> vs
     <strong>{{ breakeven.self_hosted_target }}</strong></p>
  <div class="scroll"><table>
  <thead><tr><th>Utilisation</th><th>Effective hourly capacity</th>
  <th>Cost/successful task</th><th>Break-even tasks/day</th><th>Break-even tokens/day</th></tr></thead>
  <tbody>
  {% for s in breakeven.scenarios %}
  <tr><td>{{ '%.0f'|format(s.utilization*100) }}%</td>
    <td>{{ fmt_money(s.hourly_capacity_cost, breakeven.currency) }}/h</td>
    <td>{{ fmt_money(s.cost_per_successful_task, breakeven.currency) }}</td>
    <td>{{ '%.0f'|format(s.break_even_tasks_per_day) if s.break_even_tasks_per_day else '—' }}</td>
    <td>{{ '%.1fM'|format(s.break_even_tokens_per_day/1000000) if s.break_even_tokens_per_day else '—' }}</td>
  </tr>
  {% endfor %}
  </tbody></table></div>
  <div class="note">{{ breakeven.conclusion }}</div>
  {% for c in breakeven.caveats %}<div class="legend">&bull; {{ c }}</div>{% endfor %}
</div>
{% endif %}

<h2>Per-task detail</h2>
<div class="panel scroll"><table>
<thead><tr><th>Task</th><th>Target</th><th>Runs</th><th>Success</th><th>Score</th>
<th>Mean time</th><th>&sigma; time</th><th>Mean cost</th><th>Tool calls</th></tr></thead>
<tbody>
{% for c in comp.cells %}
<tr><td class="mono">{{ c.task_id }}</td><td>{{ c.target_id }}</td><td>{{ c.runs }}</td>
  <td class="{{ 'ok' if c.success_rate >= 0.8 else ('warn' if c.success_rate >= 0.5 else 'bad') }}">
    {{ '%.0f'|format(c.success_rate*100) }}%</td>
  <td>{{ '%.3f'|format(c.mean_score or 0) }}</td>
  <td>{{ fmt_secs(c.mean_duration_s) }}</td>
  <td class="muted">{{ fmt_secs(c.stdev_duration_s) }}</td>
  <td>{{ fmt_money(c.mean_cost, 'USD') }}</td>
  <td>{{ '%.1f'|format(c.mean_tool_calls or 0) }}</td>
</tr>
{% endfor %}
</tbody></table></div>

<footer>
Generated by <span class="mono">acb</span> {{ version }}. Composite scores are shown
alongside the raw metrics that produce them; read both. Confidence intervals are Wilson
score intervals at 95%. Costs use each run's stored pricing snapshot.
</footer>
</div></body></html>
"""


def _fmt_money(value: float | None, currency: str = "USD") -> str:
    if value is None:
        return "—"
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, f"{currency} ")
    if value == 0:
        return f"{symbol}0"
    if abs(value) < 0.01:
        return f"{symbol}{value:.4f}"
    if abs(value) < 100:
        return f"{symbol}{value:.2f}"
    return f"{symbol}{value:,.0f}"


def _fmt_secs(value: float | None) -> str:
    if value is None:
        return "—"
    if value < 60:
        return f"{value:.1f}s"
    return f"{int(value // 60)}m {int(value % 60)}s"


def _scatter(comp: Comparison) -> dict:
    points = []
    priced = [
        t
        for t in comp.targets.values()
        if t.cost_per_successful_task is not None and t.cost_per_successful_task > 0
    ]
    if not priced:
        return {"points": [], "gridlines": [], "currency": "USD"}

    costs = [t.cost_per_successful_task for t in priced]
    lo, hi = min(costs), max(costs)
    span = (hi - lo) or max(hi, 1e-9)

    for t in priced:
        # x: cost (left = cheap), y: success rate (top = good)
        x = 60 + 590 * ((t.cost_per_successful_task - lo) / span if hi > lo else 0.5)
        y = 310 - 280 * t.success_rate
        points.append({"x": round(x, 1), "y": round(y, 1), "label": t.target_id})

    gridlines = [
        {"y": round(310 - 280 * frac, 1), "label": f"{int(frac * 100)}%"}
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0)
    ]
    return {"points": points, "gridlines": gridlines, "currency": priced[0].currency}


def _mcp_rows(comp: Comparison, runs: list | None) -> list[dict]:
    if not runs:
        return []
    rows = []
    for target_id, summary in comp.targets.items():
        mcp_runs = [r for r in runs if r.target_id == target_id and r.task_category == "mcp"]
        if not mcp_runs:
            continue
        successes = sum(1 for r in mcp_runs if r.result.success)
        rows.append(
            {
                "target_id": target_id,
                "success_rate": successes / len(mcp_runs),
                "calls_per_success": (
                    sum(r.tools.mcp_calls for r in mcp_runs) / successes if successes else 0.0
                ),
                "redundant": sum(r.tools.mcp_redundant_calls for r in mcp_runs),
                "missing": sum(len(r.tools.mcp_missing_tools) for r in mcp_runs),
                "forbidden": sum(len(r.tools.mcp_forbidden_tools_used) for r in mcp_runs),
                "_summary": summary,
            }
        )
    return rows


def render_html(
    comp: Comparison,
    breakeven: BreakEvenAnalysis | None = None,
    runs: list | None = None,
) -> str:
    from acb import __version__

    env = Environment(autoescape=select_autoescape(["html"]))
    env.globals["fmt_money"] = _fmt_money
    env.globals["fmt_secs"] = _fmt_secs
    template = env.from_string(_TEMPLATE)

    targets = sorted(
        comp.targets.values(), key=lambda t: (-t.success_rate, -(t.score.mean or 0.0))
    )
    best_quality = targets[0] if targets else None
    priced = [t for t in targets if t.cost_per_successful_task is not None]
    best_value = min(priced, key=lambda t: t.cost_per_successful_task) if priced else None
    timed = [t for t in targets if t.duration.median is not None]
    fastest = min(timed, key=lambda t: t.duration.median) if timed else None

    warnings: list[str] = []
    for t in targets:
        if t.cost_incomplete:
            warnings.append(
                f"{t.target_id}: cost is incomplete (missing rates or usage fields) and "
                "should be read as a lower bound."
            )
        if t.runs < 3:
            warnings.append(
                f"{t.target_id}: only {t.runs} run(s) — too few to distinguish model "
                "quality from run-to-run variance."
            )

    return template.render(
        comp=comp,
        targets=targets,
        version=__version__,
        breakeven=breakeven,
        warnings=warnings,
        scatter=_scatter(comp),
        mcp_targets=_mcp_rows(comp, runs),
        best_quality_name=best_quality.target_id if best_quality else "—",
        best_quality_rate=best_quality.success_rate if best_quality else 0.0,
        best_value_name=best_value.target_id if best_value else "—",
        best_value_cost=(
            _fmt_money(best_value.cost_per_successful_task, best_value.currency) + " per success"
            if best_value
            else "no priced runs"
        ),
        fastest_name=fastest.target_id if fastest else "—",
        fastest_time=_fmt_secs(fastest.duration.median) if fastest else "—",
    )


def render_csv(comp: Comparison) -> str:
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "target", "model", "provider", "deployment", "pricing_type", "runs", "successes",
            "success_rate", "ci_low", "ci_high", "mean_score", "median_duration_s",
            "p90_duration_s", "total_cost", "mean_cost", "cost_per_successful_task",
            "mean_tool_calls", "failed_calls_per_run", "repeated_reads_per_run",
            "input_tokens", "output_tokens", "cached_input_tokens", "cost_incomplete",
        ]
    )
    for t in sorted(comp.targets.values(), key=lambda x: x.target_id):
        writer.writerow(
            [
                t.target_id, t.model, t.provider, t.deployment_type, t.pricing_type,
                t.runs, t.successes, f"{t.success_rate:.4f}",
                f"{t.success_rate_ci[0]:.4f}", f"{t.success_rate_ci[1]:.4f}",
                f"{t.score.mean:.4f}" if t.score.mean is not None else "",
                f"{t.duration.median:.2f}" if t.duration.median is not None else "",
                f"{t.duration.p90:.2f}" if t.duration.p90 is not None else "",
                f"{t.total_cost:.6f}" if t.total_cost is not None else "",
                f"{t.cost.mean:.6f}" if t.cost.mean is not None else "",
                f"{t.cost_per_successful_task:.6f}"
                if t.cost_per_successful_task is not None else "",
                f"{t.tool_calls.mean:.2f}" if t.tool_calls.mean is not None else "",
                f"{t.failed_calls_per_run:.2f}", f"{t.repeated_reads_per_run:.2f}",
                t.input_tokens, t.output_tokens, t.cached_input_tokens, t.cost_incomplete,
            ]
        )
    return buf.getvalue()


def write_report(
    output_dir: Path,
    comp: Comparison,
    breakeven: BreakEvenAnalysis | None = None,
    runs: list | None = None,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "html": output_dir / "report.html",
        "json": output_dir / "report.json",
        "csv": output_dir / "report.csv",
    }
    paths["html"].write_text(render_html(comp, breakeven, runs))
    payload = comp.model_dump(mode="json")
    if breakeven:
        payload["break_even"] = breakeven.model_dump(mode="json")
    paths["json"].write_text(json.dumps(payload, indent=2))
    paths["csv"].write_text(render_csv(comp))
    return paths
