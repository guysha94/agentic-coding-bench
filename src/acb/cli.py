"""`benchmark` CLI."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from acb import __version__
from acb.adapters.registry import available_adapters, get_adapter
from acb.config import Paths, Workspace
from acb.db.repository import Database
from acb.fixtures import FixtureError, materialize, missing_fixtures, refresh_bundles
from acb.models.task import KNOWN_CATEGORIES
from acb.report.aggregate import build_comparison
from acb.report.breakeven import analyze_break_even
from acb.report.html import write_report
from acb.runner.orchestrator import Orchestrator, RunnerConfig
from acb.runner.workspace import prune_workspaces

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    # `--version` is valid on its own, so the group must be invocable without a command.
    invoke_without_command=True,
    help="Agentic coding model evaluation framework for Claude Code.",
)
list_app = typer.Typer(no_args_is_help=True, help="List configured tasks, targets and suites.")
app.add_typer(list_app, name="list")
def _console() -> Console:
    """Console that stays readable when output is piped or redirected.

    Rich falls back to 80 columns off-TTY, which truncates target ids and task names in
    exactly the situation where someone is grepping the output.
    """
    if sys.stdout.isatty():
        return Console()
    width = int(os.environ.get("COLUMNS") or 0) or 160
    return Console(width=width, soft_wrap=False)


console = _console()


def _csv_option(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [v.strip() for v in value.split(",") if v.strip()]


def _load(root: Path | None = None) -> Workspace:
    try:
        return Workspace.load(root)
    except Exception as exc:
        console.print(f"[red]Failed to load configuration:[/red] {exc}")
        raise typer.Exit(2) from exc


@app.callback()
def _main(
    ctx: typer.Context,
    version: Annotated[bool, typer.Option("--version", help="Show version and exit.")] = False,
) -> None:
    if version:
        console.print(f"acb {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


# ---------------------------------------------------------------------------- init ----
@app.command()
def init(
    root: Annotated[Path | None, typer.Option(help="Project root.")] = None,
) -> None:
    """Create directories and the database, and validate the configuration."""
    paths = Paths.resolve(root)
    for d in (paths.tasks_dir, paths.suites_dir, paths.config_dir, paths.runs_dir,
              paths.reports_dir, paths.fixtures_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Fixtures ship as git bundles, because a nested git repository cannot be committed
    # into the outer one. Unpack them into working repositories.
    try:
        for status in materialize(paths.root):
            if status.materialized:
                console.print(
                    f"[green]✓[/green] fixture {status.name} materialised "
                    f"[dim]({len(status.branches)} branch(es))[/dim]"
                )
            else:
                console.print(f"[dim]· fixture {status.name} already present[/dim]")
    except FixtureError as exc:
        console.print(f"[red]✗[/red] fixture setup failed: {exc}")
        raise typer.Exit(1) from exc

    db = Database(paths.db_path)
    applied = db.migrate()
    console.print(f"[green]✓[/green] database ready at {paths.db_path}"
                  + (f" (applied migrations: {applied})" if applied else " (up to date)"))

    ws = _load(root)
    for task in ws.tasks.values():
        db.upsert_task(task)
    for target in ws.targets.values():
        db.upsert_target(target)
    task_pks = {t.id: db.upsert_task(t) for t in ws.tasks.values()}
    for suite in ws.suites.values():
        db.upsert_suite(suite, task_pks)

    console.print(
        f"[green]✓[/green] registered {len(ws.tasks)} task(s), {len(ws.targets)} target(s), "
        f"{len(ws.suites)} suite(s)"
    )


# ---------------------------------------------------------------------------- list ----
@list_app.command("tasks")
def list_tasks(
    category: Annotated[str | None, typer.Option(help="Filter by category.")] = None,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    ws = _load(root)
    table = Table(title="Benchmark tasks", header_style="bold")
    for col in ("id", "category", "difficulty", "language", "timeout", "checks", "tags"):
        table.add_column(col)
    order = {c: i for i, c in enumerate(KNOWN_CATEGORIES)}
    for task in sorted(ws.tasks.values(), key=lambda t: (order.get(t.category, 99), t.id)):
        if category and task.category != category:
            continue
        hidden = sum(1 for v in task.validation if v.hidden)
        table.add_row(
            task.id, task.category, task.difficulty, task.language or "—",
            f"{task.timeout_minutes}m",
            f"{len(task.validation)}" + (f" ({hidden} hidden)" if hidden else ""),
            ",".join(task.tags[:3]),
        )
    console.print(table)


@list_app.command("targets")
def list_targets(root: Annotated[Path | None, typer.Option()] = None) -> None:
    ws = _load(root)
    table = Table(title="Benchmark targets", header_style="bold")
    for col in ("id", "model", "provider", "deployment", "adapter", "pricing", "credential"):
        table.add_column(col)
    for t in sorted(ws.targets.values(), key=lambda x: x.id):
        ok = t.credential_available()
        cred = (
            f"[green]{t.api_key_env} set[/green]"
            if ok and t.api_key_env
            else (
                "[yellow]ambient auth[/yellow]"
                if ok
                else f"[red]{t.api_key_env or 'none'} missing[/red]"
            )
        )
        table.add_row(
            t.id + ("" if t.enabled else " [dim](disabled)[/dim]"),
            t.model, t.provider, t.deployment_type, t.adapter, t.pricing.type, cred,
        )
    console.print(table)


@list_app.command("suites")
def list_suites(root: Annotated[Path | None, typer.Option()] = None) -> None:
    ws = _load(root)
    table = Table(title="Benchmark suites", header_style="bold")
    for col in ("id", "name", "tasks"):
        table.add_column(col)
    for s in sorted(ws.suites.values(), key=lambda x: x.id):
        table.add_row(s.id, s.name, str(len(s.tasks)))
    console.print(table)


# -------------------------------------------------------------------------- doctor ----
@app.command()
def doctor(
    targets: Annotated[str | None, typer.Option(help="Comma-separated targets to check.")] = None,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Verify the environment is ready before a benchmark starts."""
    ws = _load(root)
    problems: list[str] = []
    warnings: list[str] = []

    def check(label: str, ok: bool, detail: str = "", warn_only: bool = False) -> None:
        if ok:
            suffix = f" [dim]{detail}[/dim]" if detail else ""
            console.print(f"  [green]✓[/green] {label}{suffix}")
        elif warn_only:
            console.print(f"  [yellow]![/yellow] {label} [dim]{detail}[/dim]")
            warnings.append(f"{label}: {detail}")
        else:
            console.print(f"  [red]✗[/red] {label} [dim]{detail}[/dim]")
            problems.append(f"{label}: {detail}")

    console.print("[bold]Environment[/bold]")
    check("python >= 3.12", sys.version_info >= (3, 12), platform_version())
    claude = shutil.which("claude")
    check("claude CLI on PATH", claude is not None, claude or "install Claude Code")
    if claude:
        try:
            out = subprocess.run(
                ["claude", "--version"], capture_output=True, text=True, timeout=20
            )
            check("claude --version", out.returncode == 0, out.stdout.strip())
        except Exception as exc:
            check("claude --version", False, str(exc))
    git = shutil.which("git")
    check("git on PATH", git is not None, git or "required for workspace isolation")

    console.print("\n[bold]Configuration[/bold]")
    check("targets file", ws.paths.targets_file.exists(), str(ws.paths.targets_file))
    check("tasks loaded", bool(ws.tasks), f"{len(ws.tasks)} task(s)")
    check("adapters", True, ", ".join(available_adapters()))

    console.print("\n[bold]Fixtures[/bold]")
    for name in missing_fixtures(ws.paths.root):
        check(
            f"fixture {name}",
            False,
            "bundle present but not materialised -- run `benchmark init`",
        )
    seen: set[Path] = set()
    for task in ws.tasks.values():
        repo = (ws.paths.root / task.repository.path).resolve()
        if repo in seen:
            continue
        seen.add(repo)
        if not repo.exists():
            check(f"fixture {task.repository.path}", False, "missing")
        elif not (repo / ".git").exists():
            check(f"fixture {task.repository.path}", False, "not a git repository")
        else:
            dirty = subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                capture_output=True, text=True, timeout=30,
            ).stdout.strip()
            check(
                f"fixture {task.repository.path}",
                not dirty,
                "clean" if not dirty else "has uncommitted changes",
            )

    console.print("\n[bold]Targets[/bold]")
    selected = ws.resolve_targets(_csv_option(targets))
    for t in selected:
        try:
            adapter = get_adapter(t)
        except ValueError as exc:
            check(t.id, False, str(exc))
            continue
        missing = [v for v in adapter.required_env_vars() if not os.environ.get(v)]
        if missing:
            check(t.id, False, f"missing env: {', '.join(missing)}", warn_only=not t.enabled)
        elif not t.credential_available():
            check(
                t.id, False,
                "no credential and config-dir isolation is on — a fresh CLAUDE_CONFIG_DIR "
                "drops subscription auth (see docs/limitations.md)",
            )
        else:
            detail = t.pricing.type
            if t.pricing.type == "token":
                gaps = t.pricing.missing_rates()
                if gaps:
                    check(t.id, False, f"pricing incomplete: {', '.join(gaps)}", warn_only=True)
                    continue
            check(t.id, True, detail)
        if not t.isolate_config_dir:
            warnings.append(
                f"{t.id}: config-dir isolation disabled — ambient tools/MCP/plugins may leak "
                "into runs and reduce comparability"
            )

    console.print("\n[bold]Database[/bold]")
    try:
        db = Database(ws.paths.db_path)
        db.migrate()
        check("database", True, f"{db.count_runs()} run(s) stored")
    except Exception as exc:
        check("database", False, str(exc))

    console.print()
    for w in warnings:
        console.print(f"[yellow]warning:[/yellow] {w}")
    if problems:
        console.print(f"\n[red]{len(problems)} problem(s) found.[/red]")
        raise typer.Exit(1)
    console.print("[green]Environment is ready.[/green]")


def platform_version() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"


# ----------------------------------------------------------------------------- run ----
@app.command()
def run(
    task: Annotated[str | None, typer.Option(help="Task id(s), comma-separated.")] = None,
    suite: Annotated[str | None, typer.Option(help="Suite id.")] = None,
    target: Annotated[str | None, typer.Option(help="Single target id.")] = None,
    targets: Annotated[str | None, typer.Option(help="Target ids, comma-separated.")] = None,
    runs: Annotated[int, typer.Option(help="Repetitions per (task, target).")] = 1,
    permission_mode: Annotated[
        str, typer.Option(help="Claude Code permission mode.")
    ] = "bypassPermissions",
    keep_workspace: Annotated[bool, typer.Option(help="Keep workspaces for debugging.")] = False,
    dry_run: Annotated[bool, typer.Option(help="Show the plan without executing.")] = False,
    mock: Annotated[bool, typer.Option(help="Use the mock agent (no model calls).")] = False,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Execute benchmark runs."""
    ws = _load(root)
    try:
        task_specs = ws.resolve_tasks(_csv_option(task), suite)
        target_ids = _csv_option(targets) or ([target] if target else None)
        target_specs = ws.resolve_targets(target_ids)
    except KeyError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(2) from exc

    if not task_specs:
        console.print("[red]No tasks selected.[/red]")
        raise typer.Exit(2)
    if not target_specs:
        console.print("[red]No targets selected.[/red]")
        raise typer.Exit(2)

    total = len(task_specs) * len(target_specs) * runs
    console.print(
        f"[bold]Plan:[/bold] {len(task_specs)} task(s) x {len(target_specs)} target(s) "
        f"x {runs} run(s) = {total} runs"
    )
    if dry_run:
        table = Table(header_style="bold")
        for col in ("repetition", "task", "target", "model", "timeout"):
            table.add_column(col)
        for rep in range(1, runs + 1):
            for t in target_specs:
                for ts in task_specs:
                    table.add_row(str(rep), ts.id, t.id, t.model, f"{ts.timeout_minutes}m")
        console.print(table)
        return

    agent_runner = None
    if mock:
        from acb.runner.agent import MockAgentRunner

        agent_runner = MockAgentRunner()
        console.print("[yellow]Using the mock agent: no model calls will be made.[/yellow]")

    db = Database(ws.paths.db_path)
    db.migrate()
    orchestrator = Orchestrator(
        RunnerConfig(
            repo_root=ws.paths.root,
            runs_dir=ws.paths.runs_dir,
            scoring_weights=ws.weights,
            permission_mode=permission_mode,
            keep_workspace=keep_workspace,
        ),
        agent_runner=agent_runner,
    )

    batch_id = f"batch-{uuid.uuid4().hex[:10]}"
    console.print(f"[dim]batch {batch_id}[/dim]\n")
    completed = 0
    results = []

    # Target-major within a repetition round, so provider-side drift (rate limits,
    # capacity) spreads evenly across targets instead of hitting whichever ran last.
    for rep in range(1, runs + 1):
        for t in target_specs:
            for ts in task_specs:
                completed += 1
                console.print(
                    f"[bold]({completed}/{total})[/bold] {ts.id} → {t.id} "
                    f"[dim]rep {rep}[/dim]"
                )
                record = orchestrator.run_once(
                    ts, t, ws.task_dir(ts), repetition=rep, batch_id=batch_id
                )
                results.append(record)
                try:
                    db.save_run(record, ts, t, _tool_events(record))
                except Exception as exc:
                    console.print(f"  [red]failed to persist run:[/red] {exc}")

                colour = {
                    "success": "green", "failure": "yellow", "timeout": "red",
                    "error": "red", "setup_error": "red",
                }.get(record.outcome, "white")
                cost = (
                    f"${record.cost.total_cost:.4f}"
                    if record.cost.total_cost is not None else "cost n/a"
                )
                console.print(
                    f"  [{colour}]{record.outcome}[/{colour}] · "
                    f"{record.time.wall_clock_ms / 1000:.1f}s · "
                    f"{record.tools.total_calls} tool calls · {cost} · "
                    f"score {record.score.composite:.3f}"
                )
                if record.result.failure_reason:
                    console.print(f"  [dim]{record.result.failure_reason[:200]}[/dim]")

    successes = sum(1 for r in results if r.result.success)
    console.print(
        f"\n[bold]Done:[/bold] {successes}/{len(results)} successful · batch {batch_id}"
    )
    console.print(f"[dim]Report with: benchmark report --batch {batch_id}[/dim]")


def _tool_events(record) -> list:
    """Reload the persisted tool events for DB insertion."""
    from acb.models.run import ToolEvent

    path = Path(record.artifacts_dir) / "tool_calls.jsonl"
    if not path.exists():
        return []
    events = []
    for line in path.read_text().splitlines():
        if line.strip():
            events.append(ToolEvent.model_validate_json(line))
    return events


# ------------------------------------------------------------------------- compare ----
@app.command()
def compare(
    targets: Annotated[str | None, typer.Option(help="Target ids, comma-separated.")] = None,
    tasks: Annotated[str | None, typer.Option(help="Task ids, comma-separated.")] = None,
    categories: Annotated[str | None, typer.Option(help="Categories, comma-separated.")] = None,
    batch: Annotated[str | None, typer.Option(help="Restrict to one batch.")] = None,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Compare targets on stored runs."""
    ws = _load(root)
    db = Database(ws.paths.db_path)
    db.migrate()
    records = db.load_runs(
        _csv_option(targets), _csv_option(tasks), _csv_option(categories), batch
    )
    if not records:
        console.print("[yellow]No runs match. Run `benchmark run` first.[/yellow]")
        raise typer.Exit(1)

    comp = build_comparison(records)
    table = Table(title=f"Target comparison ({comp.total_runs} runs)", header_style="bold")
    for col in ("target", "runs", "success", "95% CI", "score", "p50 time", "cost/success",
                "tools", "failed calls/run"):
        table.add_column(col)
    for t in sorted(comp.targets.values(), key=lambda x: (-x.success_rate, x.target_id)):
        cost = (
            f"${t.cost_per_successful_task:.4f}"
            if t.cost_per_successful_task is not None else "—"
        )
        table.add_row(
            t.target_id, str(t.runs), f"{t.success_rate:.0%}",
            f"{t.success_rate_ci[0]:.0%}-{t.success_rate_ci[1]:.0%}",
            f"{t.score.mean:.3f}" if t.score.mean is not None else "—",
            f"{t.duration.p50:.1f}s" if t.duration.p50 is not None else "—",
            cost + ("*" if t.cost_is_estimate else ""),
            f"{t.tool_calls.mean:.1f}" if t.tool_calls.mean is not None else "—",
            f"{t.failed_calls_per_run:.1f}",
        )
    console.print(table)

    if comp.categories:
        cat_table = Table(title="Success rate by category", header_style="bold")
        cat_table.add_column("target")
        for c in comp.categories:
            cat_table.add_column(c)
        for t in sorted(comp.targets.values(), key=lambda x: -x.success_rate):
            row = [t.target_id]
            for c in comp.categories:
                cat = t.by_category.get(c)
                row.append(f"{cat.success_rate:.0%} ({cat.successes}/{cat.runs})" if cat else "—")
            cat_table.add_row(*row)
        console.print(cat_table)

    for rec in comp.recommendations:
        if rec.get("cheaper_and_as_good"):
            console.print(
                f"[green]routing:[/green] {rec['category']} → {rec['best_value_target']} "
                f"(cost/success ${rec['best_value_cost_per_success']:.4f}; quality gap vs "
                f"{rec['best_quality_target']} not statistically resolved)"
            )
    console.print("\n[dim]* estimate (subscription allocation or utilisation assumption)[/dim]")


# -------------------------------------------------------------------------- report ----
@app.command()
def report(
    targets: Annotated[str | None, typer.Option(help="Target ids, comma-separated.")] = None,
    categories: Annotated[str | None, typer.Option(help="Categories, comma-separated.")] = None,
    batch: Annotated[str | None, typer.Option(help="Restrict to one batch.")] = None,
    output: Annotated[Path | None, typer.Option(help="Output directory.")] = None,
    reprice_current: Annotated[
        bool, typer.Option("--reprice-current", help="Re-price historical runs at today's rates.")
    ] = False,
    api_target: Annotated[str | None, typer.Option(help="Break-even: API target.")] = None,
    self_hosted_target: Annotated[
        str | None, typer.Option(help="Break-even: self-hosted target.")
    ] = None,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Generate HTML / JSON / CSV reports."""
    ws = _load(root)
    db = Database(ws.paths.db_path)
    db.migrate()
    records = db.load_runs(_csv_option(targets), None, _csv_option(categories), batch)
    if not records:
        console.print("[yellow]No runs to report on.[/yellow]")
        raise typer.Exit(1)

    if reprice_current:
        records = _reprice(records, ws)
        console.print(
            "[yellow]Runs re-priced at TODAY's configured rates. This is a what-if: it does "
            "not reflect what these runs actually cost.[/yellow]"
        )

    comp = build_comparison(records)

    breakeven = None
    if api_target and self_hosted_target:
        try:
            api_cfg = ws.targets[api_target]
            sh_cfg = ws.targets[self_hosted_target]
        except KeyError as exc:
            console.print(f"[red]unknown target: {exc}[/red]")
            raise typer.Exit(2) from exc
        breakeven = analyze_break_even(
            api_cfg, [r for r in records if r.target_id == api_target],
            sh_cfg, [r for r in records if r.target_id == self_hosted_target],
        )

    out_dir = output or ws.paths.reports_dir
    paths = write_report(out_dir, comp, breakeven, records)
    console.print(f"[green]✓[/green] HTML  {paths['html']}")
    console.print(f"[green]✓[/green] JSON  {paths['json']}")
    console.print(f"[green]✓[/green] CSV   {paths['csv']}")


def _reprice(records: list, ws: Workspace) -> list:
    """Recompute cost for historical runs using the CURRENT pricing configuration."""
    from acb.cost.engine import compute_cost

    out = []
    for r in records:
        target = ws.targets.get(r.target_id)
        if target is None:
            out.append(r)
            continue
        clone = r.model_copy(deep=True)
        clone.cost = compute_cost(
            clone.usage, target.pricing, clone.time.wall_clock_ms / 1000.0
        )
        clone.cost.warnings.append(
            "REPRICED at current configuration; this is not the cost the run incurred"
        )
        out.append(clone)
    return out


# ---------------------------------------------------------------------- break-even ----
@app.command("break-even")
def break_even(
    api_target: Annotated[str, typer.Option(help="API-hosted target id.")],
    self_hosted_target: Annotated[str, typer.Option(help="Self-hosted target id.")],
    utilizations: Annotated[
        str, typer.Option(help="Utilisation scenarios, comma-separated fractions.")
    ] = "0.1,0.3,0.5,0.7,0.9",
    batch: Annotated[str | None, typer.Option()] = None,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Estimate the workload at which self-hosting beats API inference."""
    ws = _load(root)
    db = Database(ws.paths.db_path)
    db.migrate()
    try:
        api_cfg = ws.targets[api_target]
        sh_cfg = ws.targets[self_hosted_target]
    except KeyError as exc:
        console.print(f"[red]unknown target: {exc}[/red]")
        raise typer.Exit(2) from exc

    api_runs = db.load_runs([api_target], batch_id=batch)
    sh_runs = db.load_runs([self_hosted_target], batch_id=batch)
    analysis = analyze_break_even(
        api_cfg, api_runs, sh_cfg, sh_runs,
        [float(u) for u in utilizations.split(",") if u.strip()],
    )

    console.print(
        f"[bold]{api_target}[/bold] (API) vs "
        f"[bold]{self_hosted_target}[/bold] (self-hosted)\n"
    )
    console.print(
        f"API runs: {analysis.api_runs} ({analysis.api_successes} successful) · "
        f"cost/success: "
        + (
            f"${analysis.api_cost_per_successful_task:.4f}"
            if analysis.api_cost_per_successful_task is not None else "n/a"
        )
    )
    console.print(
        f"Self-hosted runs: {analysis.self_hosted_runs} "
        f"({analysis.self_hosted_successes} successful) · throughput: "
        + (
            f"{analysis.observed_successful_tasks_per_hour:.2f} successful tasks/hour"
            if analysis.observed_successful_tasks_per_hour is not None else "n/a"
        )
    )
    if analysis.raw_hourly_cost is not None:
        console.print(f"Configured hardware cost: ${analysis.raw_hourly_cost:.2f}/hour\n")

    table = Table(header_style="bold")
    for col in ("utilisation", "effective $/h", "$/successful task", "break-even tasks/day",
                "break-even tokens/day"):
        table.add_column(col)
    for s in analysis.scenarios:
        table.add_row(
            f"{s.utilization:.0%}",
            f"${s.hourly_capacity_cost:.2f}",
            f"${s.cost_per_successful_task:.4f}" if s.cost_per_successful_task else "—",
            f"{s.break_even_tasks_per_day:,.0f}" if s.break_even_tasks_per_day else "—",
            f"{s.break_even_tokens_per_day / 1e6:,.1f}M" if s.break_even_tokens_per_day else "—",
        )
    console.print(table)
    console.print(f"\n[bold]{analysis.conclusion}[/bold]\n")
    for c in analysis.caveats:
        console.print(f"[dim]• {c}[/dim]")


# ---------------------------------------------------------------------------- show ----
@app.command()
def show(
    run_id: Annotated[str, typer.Argument(help="Run id to inspect.")],
    artifacts: Annotated[bool, typer.Option(help="List artifact files.")] = False,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Inspect one run in detail."""
    ws = _load(root)
    db = Database(ws.paths.db_path)
    db.migrate()
    record = db.load_run(run_id)
    if record is None:
        console.print(f"[red]run {run_id} not found[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]{record.run_id}[/bold]")
    console.print(f"  task      {record.task_id} ({record.task_category}/{record.task_difficulty})")
    console.print(f"  target    {record.target_id} — {record.model} via {record.provider}")
    console.print(f"  outcome   {record.outcome}  score {record.score.composite:.3f}")
    if record.result.failure_reason:
        console.print(f"  reason    {record.result.failure_reason}")
    console.print(
        f"  time      {record.time.wall_clock_ms / 1000:.1f}s total"
        + (f", first edit at {record.time.time_to_first_edit_ms / 1000:.1f}s"
           if record.time.time_to_first_edit_ms else "")
    )
    console.print(
        f"  tokens    in {record.usage.input_tokens:,} · out {record.usage.output_tokens:,} · "
        f"cached {record.usage.cached_input_tokens:,} · cache-write "
        f"{record.usage.cache_write_tokens:,}"
    )
    cost = (
        f"${record.cost.total_cost:.4f}" if record.cost.total_cost is not None else "n/a"
    )
    console.print(f"  cost      {cost} ({record.cost.pricing_type})")
    for w in record.cost.warnings:
        console.print(f"            [yellow]! {w}[/yellow]")
    console.print(
        f"  tools     {record.tools.total_calls} calls · {record.tools.failed_calls} failed · "
        f"{record.tools.repeated_reads} repeated reads"
    )
    console.print(f"  sequence  [dim]{record.tools.sequence_string()}[/dim]")
    console.print(f"  changes   {record.result.files_changed} files "
                  f"(+{record.result.lines_added}/-{record.result.lines_removed})")

    if record.validations:
        table = Table(title="Validation", header_style="bold")
        for col in ("phase", "name", "kind", "hidden", "result", "tests"):
            table.add_column(col)
        for v in record.validations:
            tests = f"{v.tests_passed}/{v.tests_total}" if v.tests_total else "—"
            table.add_row(
                v.phase, v.name, v.kind, "yes" if v.hidden else "",
                "[green]pass[/green]" if v.passed else "[red]fail[/red]", tests,
            )
        console.print(table)

    if artifacts:
        d = Path(record.artifacts_dir)
        console.print(f"\n[bold]Artifacts[/bold] {d}")
        if d.exists():
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    console.print(f"  {p.relative_to(d)} [dim]{p.stat().st_size:,}b[/dim]")


# --------------------------------------------------------------------------- prune ----
@app.command()
def prune(
    workspaces: Annotated[bool, typer.Option(help="Remove leftover workspaces.")] = True,
) -> None:
    """Clean up leftover workspaces from crashed runs."""
    if workspaces:
        n = prune_workspaces()
        console.print(f"[green]✓[/green] removed {n} leftover workspace(s)")


@app.command()
def fixtures(
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help="Regenerate bundles FROM the working fixture repos (after editing one).",
        ),
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Re-clone fixtures, discarding local changes.")
    ] = False,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Materialise fixture repositories from their bundles, or regenerate the bundles."""
    paths = Paths.resolve(root)
    try:
        if refresh:
            for bundle in refresh_bundles(paths.root):
                console.print(f"[green]✓[/green] regenerated {bundle.relative_to(paths.root)}")
            return
        for status in materialize(paths.root, force=force):
            state = "materialised" if status.materialized else "already present"
            console.print(
                f"[green]✓[/green] {status.name} [dim]{state}; branches: "
                f"{', '.join(status.branches)}[/dim]"
            )
    except FixtureError as exc:
        console.print(f"[red]✗[/red] {exc}")
        raise typer.Exit(1) from exc


@app.command()
def validate(root: Annotated[Path | None, typer.Option()] = None) -> None:
    """Validate all task and target configuration without running anything."""
    ws = _load(root)
    errors: list[str] = []
    for task in ws.tasks.values():
        repo = (ws.paths.root / task.repository.path).resolve()
        if not repo.exists():
            errors.append(f"{task.id}: fixture missing ({task.repository.path})")
        if task.category not in KNOWN_CATEGORIES:
            console.print(f"[yellow]note:[/yellow] {task.id} uses custom category "
                          f"{task.category!r}")
        for v in task.validation:
            for src in v.provides_files.values():
                if not (ws.task_dir(task) / src).exists():
                    errors.append(f"{task.id}: check {v.name!r} provides missing file {src}")
    for target in ws.targets.values():
        try:
            get_adapter(target)
        except ValueError as exc:
            errors.append(str(exc))
    for suite in ws.suites.values():
        for t in suite.tasks:
            if t not in ws.tasks:
                errors.append(f"suite {suite.id}: unknown task {t}")

    if errors:
        for e in errors:
            console.print(f"[red]✗[/red] {e}")
        raise typer.Exit(1)
    console.print(
        f"[green]✓[/green] {len(ws.tasks)} task(s), {len(ws.targets)} target(s), "
        f"{len(ws.suites)} suite(s) valid"
    )


@app.command("export")
def export_runs(
    output: Annotated[Path, typer.Option(help="Output JSON file.")] = Path("runs-export.json"),
    batch: Annotated[str | None, typer.Option()] = None,
    root: Annotated[Path | None, typer.Option()] = None,
) -> None:
    """Export raw run records as JSON."""
    ws = _load(root)
    db = Database(ws.paths.db_path)
    db.migrate()
    records = db.load_runs(batch_id=batch)
    output.write_text(json.dumps([r.model_dump(mode="json") for r in records], indent=2))
    console.print(f"[green]✓[/green] exported {len(records)} run(s) to {output}")


if __name__ == "__main__":
    app()
