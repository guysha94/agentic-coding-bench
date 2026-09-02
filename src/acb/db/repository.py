"""Persistence for run records.

Every write is idempotent on `run_id`, and no write ever overwrites a previous run: repeated
runs of the same (task, target) are separate rows, distinguished by `repetition`.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from acb.db.migrations import migrate
from acb.db.schema import (
    BenchmarkRun,
    BenchmarkSuite,
    BenchmarkTarget,
    BenchmarkTask,
    Model,
    ModelUsageRow,
    PricingConfigRow,
    PricingSnapshotRow,
    Provider,
    RunMetricRow,
    SuiteTask,
    ToolEventRow,
    ValidationResultRow,
)
from acb.models.run import RunRecord
from acb.models.target import TargetConfig
from acb.models.task import SuiteSpec, TaskSpec


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()[:64]


class Database:
    def __init__(self, url_or_path: str | Path = "acb.sqlite3") -> None:
        if isinstance(url_or_path, Path) or "://" not in str(url_or_path):
            path = Path(url_or_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite:///{path}"
        else:
            url = str(url_or_path)
        self.url = url
        self.engine: Engine = create_engine(url, future=True)
        self._session_factory = sessionmaker(bind=self.engine, future=True)

    def migrate(self) -> list[int]:
        return migrate(self.engine)

    def session(self) -> Session:
        return self._session_factory()

    # --------------------------------------------------------------- dimension upserts --
    def _get_or_create_model(self, s: Session, name: str, family: str | None) -> Model:
        row = s.scalar(select(Model).where(Model.name == name))
        if row is None:
            row = Model(name=name, family=family, created_at=_now())
            s.add(row)
            s.flush()
        return row

    def _get_or_create_provider(self, s: Session, name: str) -> Provider:
        row = s.scalar(select(Provider).where(Provider.name == name))
        if row is None:
            row = Provider(name=name, created_at=_now())
            s.add(row)
            s.flush()
        return row

    def upsert_target(self, target: TargetConfig, s: Session | None = None) -> int:
        own = s is None
        s = s or self.session()
        try:
            model = self._get_or_create_model(s, target.model, target.model_family)
            provider = self._get_or_create_provider(s, target.provider)
            row = s.scalar(select(BenchmarkTarget).where(BenchmarkTarget.key == target.id))
            payload = target.model_dump(mode="json")
            if row is None:
                row = BenchmarkTarget(
                    key=target.id,
                    model_id=model.id,
                    provider_id=provider.id,
                    deployment_type=target.deployment_type,
                    adapter=target.adapter,
                    endpoint=target.endpoint,
                    display_name=target.display_name,
                    config_json=json.dumps(payload),
                    created_at=_now(),
                )
                s.add(row)
            else:
                row.model_id = model.id
                row.provider_id = provider.id
                row.deployment_type = target.deployment_type
                row.adapter = target.adapter
                row.endpoint = target.endpoint
                row.display_name = target.display_name
                row.config_json = json.dumps(payload)
            s.flush()
            pk = row.id
            if own:
                s.commit()
            return pk
        finally:
            if own:
                s.close()

    def upsert_pricing_config(self, target: TargetConfig, s: Session) -> int:
        payload = target.pricing.model_dump(mode="json")
        digest = _digest(payload)
        row = s.scalar(
            select(PricingConfigRow).where(
                PricingConfigRow.target_key == target.id,
                PricingConfigRow.config_digest == digest,
            )
        )
        if row is None:
            row = PricingConfigRow(
                target_key=target.id,
                pricing_type=target.pricing.type,
                currency=target.pricing.currency,
                effective_date=target.pricing.effective_date,
                config_json=json.dumps(payload),
                config_digest=digest,
                created_at=_now(),
            )
            s.add(row)
            s.flush()
        return row.id

    def upsert_task(self, task: TaskSpec, s: Session | None = None) -> int:
        own = s is None
        s = s or self.session()
        try:
            row = s.scalar(
                select(BenchmarkTask).where(
                    BenchmarkTask.key == task.id, BenchmarkTask.version == task.version
                )
            )
            payload = json.dumps(task.model_dump(mode="json"))
            if row is None:
                row = BenchmarkTask(
                    key=task.id,
                    version=task.version,
                    name=task.name,
                    category=task.category,
                    difficulty=task.difficulty,
                    language=task.language,
                    framework=task.framework,
                    timeout_minutes=task.timeout_minutes,
                    requires_network=task.requires_network,
                    spec_json=payload,
                    created_at=_now(),
                )
                s.add(row)
            else:
                row.spec_json = payload
                row.name = task.name
                row.category = task.category
            s.flush()
            pk = row.id
            if own:
                s.commit()
            return pk
        finally:
            if own:
                s.close()

    def upsert_suite(self, suite: SuiteSpec, task_pks: dict[str, int]) -> int:
        with self.session() as s:
            row = s.scalar(select(BenchmarkSuite).where(BenchmarkSuite.key == suite.id))
            if row is None:
                row = BenchmarkSuite(
                    key=suite.id,
                    name=suite.name,
                    description=suite.description,
                    created_at=_now(),
                )
                s.add(row)
                s.flush()
            for pos, task_id in enumerate(suite.tasks):
                pk = task_pks.get(task_id)
                if pk is None:
                    continue
                link = s.scalar(
                    select(SuiteTask).where(
                        SuiteTask.suite_id == row.id, SuiteTask.task_id == pk
                    )
                )
                if link is None:
                    s.add(SuiteTask(suite_id=row.id, task_id=pk, position=pos))
                else:
                    link.position = pos
            s.commit()
            return row.id

    # ------------------------------------------------------------------- run persistence --
    def save_run(
        self,
        record: RunRecord,
        task: TaskSpec,
        target: TargetConfig,
        tool_events: list[Any] | None = None,
        raw_usage_available: bool = False,
    ) -> int:
        with self.session() as s:
            existing = s.scalar(select(BenchmarkRun).where(BenchmarkRun.run_id == record.run_id))
            if existing is not None:
                # Runs are immutable facts; a duplicate id is a bug, not an update.
                raise ValueError(f"run {record.run_id} already persisted")

            task_pk = self.upsert_task(task, s)
            target_pk = self.upsert_target(target, s)
            pricing_pk = self.upsert_pricing_config(target, s)

            run = BenchmarkRun(
                run_id=record.run_id,
                batch_id=record.batch_id,
                task_id=task_pk,
                target_id=target_pk,
                pricing_config_id=pricing_pk,
                task_key=record.task_id,
                task_category=record.task_category,
                task_difficulty=record.task_difficulty,
                target_key=record.target_id,
                model_name=record.model,
                model_family=record.model_family,
                provider_name=record.provider,
                deployment_type=record.deployment_type,
                repetition=record.repetition,
                started_at=record.started_at,
                finished_at=record.finished_at,
                outcome=record.outcome,
                success=record.result.success,
                timed_out=record.timed_out,
                error=record.error,
                terminal_reason=record.terminal_reason,
                agent_exit_code=record.agent_exit_code,
                num_turns=record.num_turns,
                permission_denials=record.permission_denials,
                wall_clock_ms=record.time.wall_clock_ms,
                total_cost=record.cost.total_cost,
                composite_score=record.score.composite,
                artifacts_dir=record.artifacts_dir,
                environment_json=json.dumps(record.environment.model_dump(mode="json")),
                record_json=json.dumps(record.model_dump(mode="json")),
            )
            s.add(run)
            s.flush()

            for ev in tool_events or []:
                s.add(
                    ToolEventRow(
                        run_pk=run.id,
                        seq=ev.index,
                        tool_use_id=ev.tool_use_id,
                        name=ev.name,
                        kind=ev.kind,
                        input_digest=ev.input_digest,
                        input_summary=ev.input_summary,
                        target_path=ev.target_path,
                        started_at=ev.started_at,
                        completed_at=ev.completed_at,
                        duration_ms=ev.duration_ms,
                        is_error=ev.is_error,
                        is_repeat=ev.is_repeat,
                        mcp_server=ev.mcp_server,
                        parent_tool_use_id=ev.parent_tool_use_id,
                    )
                )

            for v in record.validations:
                s.add(
                    ValidationResultRow(
                        run_pk=run.id,
                        name=v.name,
                        kind=v.kind,
                        phase=v.phase,
                        hidden=v.hidden,
                        passed=v.passed,
                        exit_code=v.exit_code,
                        duration_ms=v.duration_ms,
                        timed_out=v.timed_out,
                        tests_passed=v.tests_passed,
                        tests_failed=v.tests_failed,
                        tests_total=v.tests_total,
                        error=v.error,
                    )
                )

            s.add(
                ModelUsageRow(
                    run_pk=run.id,
                    model_name=record.model,
                    input_tokens=record.usage.input_tokens,
                    output_tokens=record.usage.output_tokens,
                    cached_input_tokens=record.usage.cached_input_tokens,
                    cache_write_tokens=record.usage.cache_write_tokens,
                    reasoning_tokens=record.usage.reasoning_tokens,
                    total_turns=record.usage.total_turns,
                    dialect=record.usage.dialect,
                    normalization_version=record.usage.normalization_version,
                    complete=record.usage.complete,
                    normalized_json=json.dumps(record.usage.model_dump(mode="json")),
                    raw_available=raw_usage_available,
                )
            )

            s.add(
                PricingSnapshotRow(
                    run_pk=run.id,
                    pricing_type=record.cost.pricing_type,
                    currency=record.cost.currency,
                    effective_date=record.pricing_snapshot.get("pricing_effective_date"),
                    normalization_version=record.pricing_snapshot.get("normalization_version"),
                    snapshot_json=json.dumps(record.pricing_snapshot),
                    cost_json=json.dumps(record.cost.model_dump(mode="json")),
                )
            )

            for group, metrics in (
                ("tools", record.tools.model_dump(mode="json")),
                ("time", record.time.model_dump(mode="json")),
                ("outcome", record.result.model_dump(mode="json")),
                ("score", record.score.model_dump(mode="json")),
                ("usage", record.usage.model_dump(mode="json")),
            ):
                for metric, value in metrics.items():
                    if isinstance(value, (bool, int, float)):
                        s.add(
                            RunMetricRow(
                                run_pk=run.id,
                                group_name=group,
                                metric=metric,
                                value=float(value),
                            )
                        )
                    elif isinstance(value, str) and value:
                        s.add(
                            RunMetricRow(
                                run_pk=run.id,
                                group_name=group,
                                metric=metric,
                                text_value=value[:2000],
                            )
                        )
            s.commit()
            return run.id

    # ------------------------------------------------------------------------- queries --
    def load_runs(
        self,
        targets: list[str] | None = None,
        tasks: list[str] | None = None,
        categories: list[str] | None = None,
        batch_id: str | None = None,
    ) -> list[RunRecord]:
        with self.session() as s:
            stmt = select(BenchmarkRun)
            if targets:
                stmt = stmt.where(BenchmarkRun.target_key.in_(targets))
            if tasks:
                stmt = stmt.where(BenchmarkRun.task_key.in_(tasks))
            if categories:
                stmt = stmt.where(BenchmarkRun.task_category.in_(categories))
            if batch_id:
                stmt = stmt.where(BenchmarkRun.batch_id == batch_id)
            stmt = stmt.order_by(BenchmarkRun.started_at)
            return [RunRecord.model_validate_json(r.record_json) for r in s.scalars(stmt)]

    def load_run(self, run_id: str) -> RunRecord | None:
        with self.session() as s:
            row = s.scalar(select(BenchmarkRun).where(BenchmarkRun.run_id == run_id))
            return RunRecord.model_validate_json(row.record_json) if row else None

    def count_runs(self) -> int:
        with self.session() as s:
            return len(list(s.scalars(select(BenchmarkRun.id))))

    def list_batches(self) -> list[str]:
        with self.session() as s:
            rows = s.scalars(select(BenchmarkRun.batch_id).distinct())
            return sorted({r for r in rows if r})
