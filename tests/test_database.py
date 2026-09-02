"""Database persistence, migrations, and query paths."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from acb.db.migrations import MIGRATIONS, applied_versions, current_version, migrate
from acb.db.repository import Database
from acb.models.run import RunRecord, ToolEvent, ValidationResult
from acb.models.task import SuiteSpec
from acb.models.usage import CostBreakdown, NormalizedUsage


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "test.sqlite3")
    database.migrate()
    return database


def make_record(run_id: str = "run-1", **overrides) -> RunRecord:
    record = RunRecord(
        run_id=run_id,
        task_id="test-task-001",
        task_category="bug_fix",
        task_difficulty="easy",
        target_id="test-anthropic",
        model="opus",
        model_family="claude-opus",
        provider="anthropic",
        usage=NormalizedUsage(
            input_tokens=1000, output_tokens=200, cached_input_tokens=5000,
            cache_write_tokens=100, total_turns=4, dialect="anthropic_v1",
        ),
        cost=CostBreakdown(pricing_type="token", total_cost=0.05, input_cost=0.003),
        validations=[
            ValidationResult(name="unit", kind="test", command="pytest", hidden=False,
                             phase="post", passed=True, tests_passed=10, tests_total=10)
        ],
    )
    for key, value in overrides.items():
        setattr(record, key, value)
    return record


class TestMigrations:
    def test_migration_creates_every_table(self, tmp_path: Path):
        database = Database(tmp_path / "m.sqlite3")
        applied = database.migrate()
        assert applied == [m.version for m in MIGRATIONS]
        tables = set(inspect(database.engine).get_table_names())
        for expected in (
            "models", "providers", "benchmark_targets", "pricing_configs", "benchmark_tasks",
            "benchmark_suites", "benchmark_runs", "tool_events", "validation_results",
            "run_metrics", "model_usage", "pricing_snapshots", "schema_migrations",
        ):
            assert expected in tables, f"missing table {expected}"

    def test_migration_is_idempotent(self, tmp_path: Path):
        database = Database(tmp_path / "m.sqlite3")
        assert database.migrate()
        assert database.migrate() == [], "re-running migrations must be a no-op"

    def test_version_is_tracked(self, db: Database):
        assert current_version(db.engine) == max(m.version for m in MIGRATIONS)
        assert applied_versions(db.engine) == {m.version for m in MIGRATIONS}

    def test_fresh_engine_reports_version_zero(self, tmp_path: Path):
        from sqlalchemy import create_engine

        engine = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite3'}")
        assert current_version(engine) == 0
        migrate(engine)
        assert current_version(engine) > 0


class TestPersistence:
    def test_save_and_reload_roundtrip(self, db, sample_task, anthropic_target):
        db.save_run(make_record(), sample_task, anthropic_target)
        loaded = db.load_run("run-1")
        assert loaded is not None
        assert loaded.task_id == "test-task-001"
        assert loaded.usage.input_tokens == 1000
        assert loaded.cost.total_cost == 0.05

    def test_duplicate_run_id_is_rejected(self, db, sample_task, anthropic_target):
        """Runs are immutable facts; a repeat id would silently overwrite evidence."""
        db.save_run(make_record(), sample_task, anthropic_target)
        with pytest.raises(ValueError, match="already persisted"):
            db.save_run(make_record(), sample_task, anthropic_target)

    def test_repeated_runs_are_stored_independently(self, db, sample_task, anthropic_target):
        for i in range(1, 4):
            db.save_run(make_record(f"run-{i}", repetition=i), sample_task, anthropic_target)
        assert db.count_runs() == 3
        assert {r.repetition for r in db.load_runs()} == {1, 2, 3}

    def test_tool_events_are_persisted(self, db, sample_task, anthropic_target):
        events = [
            ToolEvent(index=0, name="Read", kind="read", target_path="/a.py"),
            ToolEvent(index=1, name="Bash", kind="bash", is_error=True),
        ]
        db.save_run(make_record(), sample_task, anthropic_target, events)
        with db.session() as s:
            rows = list(
                s.execute(text("SELECT name, kind, is_error FROM tool_events ORDER BY seq"))
            )
        assert [r[0] for r in rows] == ["Read", "Bash"]
        assert rows[1][2] == 1

    def test_validation_results_are_persisted(self, db, sample_task, anthropic_target):
        db.save_run(make_record(), sample_task, anthropic_target)
        with db.session() as s:
            rows = list(s.execute(text("SELECT name, passed, tests_total FROM validation_results")))
        assert rows == [("unit", 1, 10)]

    def test_usage_row_records_the_normalisation_version(self, db, sample_task, anthropic_target):
        db.save_run(make_record(), sample_task, anthropic_target)
        with db.session() as s:
            row = s.execute(
                text("SELECT input_tokens, cached_input_tokens, dialect, normalization_version "
                     "FROM model_usage")
            ).one()
        assert row[0] == 1000
        assert row[1] == 5000
        assert row[2] == "anthropic_v1"
        assert row[3]

    def test_pricing_snapshot_is_persisted_per_run(self, db, sample_task, anthropic_target):
        record = make_record()
        record.pricing_snapshot = anthropic_target.pricing_snapshot()
        db.save_run(record, sample_task, anthropic_target)
        with db.session() as s:
            snapshot_json, = s.execute(text("SELECT snapshot_json FROM pricing_snapshots")).one()
        snapshot = json.loads(snapshot_json)
        assert snapshot["pricing_snapshot"]["input_per_million_tokens"] == 3.0

    def test_metrics_are_stored_in_long_format(self, db, sample_task, anthropic_target):
        """Long format means a new metric needs no schema migration."""
        record = make_record()
        record.tools.total_calls = 17
        db.save_run(record, sample_task, anthropic_target)
        with db.session() as s:
            value, = s.execute(
                text("SELECT value FROM run_metrics WHERE group_name='tools' "
                     "AND metric='total_calls'")
            ).one()
        assert value == 17.0


class TestDimensions:
    def test_target_upsert_is_idempotent(self, db, anthropic_target):
        first = db.upsert_target(anthropic_target)
        assert db.upsert_target(anthropic_target) == first
        with db.session() as s:
            count, = s.execute(text("SELECT COUNT(*) FROM benchmark_targets")).one()
        assert count == 1

    def test_target_upsert_creates_model_and_provider(self, db, anthropic_target):
        db.upsert_target(anthropic_target)
        with db.session() as s:
            models = [r[0] for r in s.execute(text("SELECT name FROM models"))]
            providers = [r[0] for r in s.execute(text("SELECT name FROM providers"))]
        assert models == ["opus"]
        assert providers == ["anthropic"]

    def test_task_versions_are_stored_separately(self, db, sample_task):
        first = db.upsert_task(sample_task)
        sample_task.version = 2
        second = db.upsert_task(sample_task)
        assert first != second

    def test_changing_pricing_creates_a_new_config_row(self, db, sample_task, anthropic_target):
        db.save_run(make_record("r1"), sample_task, anthropic_target)
        anthropic_target.pricing.input_per_million_tokens = 99.0
        db.save_run(make_record("r2"), sample_task, anthropic_target)
        with db.session() as s:
            count, = s.execute(text("SELECT COUNT(*) FROM pricing_configs")).one()
        assert count == 2, "a price change must be recorded as a distinct config"

    def test_suite_upsert(self, db, sample_task):
        task_pk = db.upsert_task(sample_task)
        suite = SuiteSpec(id="s1", name="Suite", tasks=[sample_task.id])
        db.upsert_suite(suite, {sample_task.id: task_pk})
        db.upsert_suite(suite, {sample_task.id: task_pk})  # idempotent
        with db.session() as s:
            links, = s.execute(text("SELECT COUNT(*) FROM suite_tasks")).one()
        assert links == 1


class TestQueries:
    @pytest.fixture
    def populated(self, db, sample_task, anthropic_target, openai_target):
        db.save_run(make_record("a1", batch_id="b1"), sample_task, anthropic_target)
        db.save_run(
            make_record("o1", target_id="test-openai", provider="fireworks", batch_id="b1"),
            sample_task, openai_target,
        )
        db.save_run(
            make_record("a2", batch_id="b2", task_category="feature"),
            sample_task, anthropic_target,
        )
        return db

    def test_filter_by_target(self, populated):
        assert {r.run_id for r in populated.load_runs(targets=["test-anthropic"])} == {"a1", "a2"}

    def test_filter_by_category(self, populated):
        assert {r.run_id for r in populated.load_runs(categories=["feature"])} == {"a2"}

    def test_filter_by_batch(self, populated):
        assert {r.run_id for r in populated.load_runs(batch_id="b1")} == {"a1", "o1"}

    def test_filter_by_task(self, populated):
        assert len(populated.load_runs(tasks=["test-task-001"])) == 3

    def test_list_batches(self, populated):
        assert populated.list_batches() == ["b1", "b2"]

    def test_missing_run_returns_none(self, db):
        assert db.load_run("nope") is None


class TestPortability:
    def test_no_sqlite_specific_column_types(self, db):
        """The schema must port to PostgreSQL without rewriting column types."""
        inspector = inspect(db.engine)
        allowed = {"INTEGER", "VARCHAR", "TEXT", "FLOAT", "BOOLEAN", "BIGINT", "NUMERIC"}
        for table in inspector.get_table_names():
            for column in inspector.get_columns(table):
                base = str(column["type"]).split("(")[0].upper()
                assert base in allowed, f"{table}.{column['name']} uses {base}"

    def test_timestamps_are_iso_strings(self, db, sample_task, anthropic_target):
        db.save_run(make_record(), sample_task, anthropic_target)
        with db.session() as s:
            started, = s.execute(text("SELECT started_at FROM benchmark_runs")).one()
        assert isinstance(started, str)
        from datetime import datetime

        datetime.fromisoformat(started)  # must parse

    def test_accepts_a_full_database_url(self, tmp_path: Path):
        database = Database(f"sqlite:///{tmp_path / 'url.sqlite3'}")
        database.migrate()
        assert database.count_runs() == 0
