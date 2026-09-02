"""Database schema.

SQLite first, written to port cleanly to PostgreSQL:

* integer surrogate primary keys, no SQLite-only types
* timestamps as ISO-8601 UTC text (portable, sortable, unambiguous)
* JSON payloads in TEXT columns, serialised by the repository layer
* no reliance on SQLite's dynamic typing

Artifacts stay on disk under `runs/<run-id>/` and are referenced by path -- the database
holds facts and metrics, not blobs.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Model(Base):
    __tablename__ = "models"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    family = Column(String(100))
    created_at = Column(String(40), nullable=False)


class Provider(Base):
    __tablename__ = "providers"
    id = Column(Integer, primary_key=True)
    name = Column(String(100), nullable=False, unique=True)
    created_at = Column(String(40), nullable=False)


class PricingConfigRow(Base):
    __tablename__ = "pricing_configs"
    id = Column(Integer, primary_key=True)
    target_key = Column(String(200), nullable=False)
    pricing_type = Column(String(40), nullable=False)
    currency = Column(String(10), nullable=False, default="USD")
    effective_date = Column(String(40))
    config_json = Column(Text, nullable=False)
    config_digest = Column(String(64), nullable=False)
    created_at = Column(String(40), nullable=False)
    __table_args__ = (
        UniqueConstraint("target_key", "config_digest", name="uq_pricing_target_digest"),
    )


class BenchmarkTarget(Base):
    __tablename__ = "benchmark_targets"
    id = Column(Integer, primary_key=True)
    key = Column(String(200), nullable=False, unique=True)
    model_id = Column(Integer, ForeignKey("models.id"), nullable=False)
    provider_id = Column(Integer, ForeignKey("providers.id"), nullable=False)
    deployment_type = Column(String(40), nullable=False)
    adapter = Column(String(60), nullable=False)
    endpoint = Column(String(500))
    display_name = Column(String(200))
    config_json = Column(Text, nullable=False)
    created_at = Column(String(40), nullable=False)

    model = relationship("Model")
    provider = relationship("Provider")


class BenchmarkTask(Base):
    __tablename__ = "benchmark_tasks"
    id = Column(Integer, primary_key=True)
    key = Column(String(200), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    name = Column(String(300), nullable=False)
    category = Column(String(60), nullable=False)
    difficulty = Column(String(20), nullable=False)
    language = Column(String(60))
    framework = Column(String(100))
    timeout_minutes = Column(Integer, nullable=False)
    requires_network = Column(Boolean, nullable=False, default=False)
    spec_json = Column(Text, nullable=False)
    created_at = Column(String(40), nullable=False)
    __table_args__ = (UniqueConstraint("key", "version", name="uq_task_key_version"),)


class BenchmarkSuite(Base):
    __tablename__ = "benchmark_suites"
    id = Column(Integer, primary_key=True)
    key = Column(String(200), nullable=False, unique=True)
    name = Column(String(300), nullable=False)
    description = Column(Text, default="")
    created_at = Column(String(40), nullable=False)


class SuiteTask(Base):
    __tablename__ = "suite_tasks"
    id = Column(Integer, primary_key=True)
    suite_id = Column(Integer, ForeignKey("benchmark_suites.id"), nullable=False)
    task_id = Column(Integer, ForeignKey("benchmark_tasks.id"), nullable=False)
    position = Column(Integer, nullable=False, default=0)
    __table_args__ = (UniqueConstraint("suite_id", "task_id", name="uq_suite_task"),)


class BenchmarkRun(Base):
    """Fact table. One row per agent session."""

    __tablename__ = "benchmark_runs"
    id = Column(Integer, primary_key=True)
    run_id = Column(String(80), nullable=False, unique=True)
    batch_id = Column(String(80))
    task_id = Column(Integer, ForeignKey("benchmark_tasks.id"), nullable=False)
    target_id = Column(Integer, ForeignKey("benchmark_targets.id"), nullable=False)
    pricing_config_id = Column(Integer, ForeignKey("pricing_configs.id"))

    task_key = Column(String(200), nullable=False)
    task_category = Column(String(60), nullable=False)
    task_difficulty = Column(String(20))
    target_key = Column(String(200), nullable=False)
    model_name = Column(String(200), nullable=False)
    model_family = Column(String(100))
    provider_name = Column(String(100), nullable=False)
    deployment_type = Column(String(40), nullable=False)
    repetition = Column(Integer, nullable=False, default=1)

    started_at = Column(String(40), nullable=False)
    finished_at = Column(String(40))
    outcome = Column(String(20), nullable=False)
    success = Column(Boolean, nullable=False, default=False)
    timed_out = Column(Boolean, nullable=False, default=False)
    error = Column(Text)
    terminal_reason = Column(String(80))
    agent_exit_code = Column(Integer)
    num_turns = Column(Integer, nullable=False, default=0)
    permission_denials = Column(Integer, nullable=False, default=0)

    wall_clock_ms = Column(Integer, nullable=False, default=0)
    total_cost = Column(Float)
    composite_score = Column(Float, nullable=False, default=0.0)
    artifacts_dir = Column(String(500))
    environment_json = Column(Text)
    record_json = Column(Text, nullable=False)

    task = relationship("BenchmarkTask")
    target = relationship("BenchmarkTarget")
    tool_events = relationship("ToolEventRow", back_populates="run", cascade="all, delete-orphan")
    validations = relationship(
        "ValidationResultRow", back_populates="run", cascade="all, delete-orphan"
    )
    metrics = relationship("RunMetricRow", back_populates="run", cascade="all, delete-orphan")
    usage = relationship("ModelUsageRow", back_populates="run", cascade="all, delete-orphan")
    snapshots = relationship(
        "PricingSnapshotRow", back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("ix_runs_task_target", "task_key", "target_key"),
        Index("ix_runs_batch", "batch_id"),
        Index("ix_runs_category", "task_category"),
    )


class ToolEventRow(Base):
    __tablename__ = "tool_events"
    id = Column(Integer, primary_key=True)
    run_pk = Column(Integer, ForeignKey("benchmark_runs.id"), nullable=False)
    seq = Column(Integer, nullable=False)
    tool_use_id = Column(String(120))
    name = Column(String(200), nullable=False)
    kind = Column(String(40), nullable=False)
    input_digest = Column(String(64))
    input_summary = Column(Text)
    target_path = Column(String(1000))
    started_at = Column(String(40))
    completed_at = Column(String(40))
    duration_ms = Column(Integer)
    is_error = Column(Boolean, nullable=False, default=False)
    is_repeat = Column(Boolean, nullable=False, default=False)
    mcp_server = Column(String(200))
    parent_tool_use_id = Column(String(120))

    run = relationship("BenchmarkRun", back_populates="tool_events")
    __table_args__ = (Index("ix_tool_events_run_seq", "run_pk", "seq"),)


class ValidationResultRow(Base):
    __tablename__ = "validation_results"
    id = Column(Integer, primary_key=True)
    run_pk = Column(Integer, ForeignKey("benchmark_runs.id"), nullable=False)
    name = Column(String(200), nullable=False)
    kind = Column(String(40), nullable=False)
    phase = Column(String(20), nullable=False)
    hidden = Column(Boolean, nullable=False, default=False)
    passed = Column(Boolean, nullable=False, default=False)
    exit_code = Column(Integer)
    duration_ms = Column(Integer, nullable=False, default=0)
    timed_out = Column(Boolean, nullable=False, default=False)
    tests_passed = Column(Integer)
    tests_failed = Column(Integer)
    tests_total = Column(Integer)
    error = Column(Text)

    run = relationship("BenchmarkRun", back_populates="validations")


class RunMetricRow(Base):
    """Long-format metrics, so new metrics need no schema migration."""

    __tablename__ = "run_metrics"
    id = Column(Integer, primary_key=True)
    run_pk = Column(Integer, ForeignKey("benchmark_runs.id"), nullable=False)
    group_name = Column(String(60), nullable=False)
    metric = Column(String(120), nullable=False)
    value = Column(Float)
    text_value = Column(Text)

    run = relationship("BenchmarkRun", back_populates="metrics")
    __table_args__ = (
        UniqueConstraint("run_pk", "group_name", "metric", name="uq_run_metric"),
        Index("ix_run_metrics_metric", "metric"),
    )


class ModelUsageRow(Base):
    __tablename__ = "model_usage"
    id = Column(Integer, primary_key=True)
    run_pk = Column(Integer, ForeignKey("benchmark_runs.id"), nullable=False)
    model_name = Column(String(200), nullable=False)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cached_input_tokens = Column(Integer, nullable=False, default=0)
    cache_write_tokens = Column(Integer, nullable=False, default=0)
    reasoning_tokens = Column(Integer, nullable=False, default=0)
    total_turns = Column(Integer, nullable=False, default=0)
    dialect = Column(String(60))
    normalization_version = Column(String(40))
    complete = Column(Boolean, nullable=False, default=True)
    normalized_json = Column(Text)
    raw_available = Column(Boolean, nullable=False, default=False)

    run = relationship("BenchmarkRun", back_populates="usage")


class PricingSnapshotRow(Base):
    """Frozen pricing for one run, so historical results never silently re-price."""

    __tablename__ = "pricing_snapshots"
    id = Column(Integer, primary_key=True)
    run_pk = Column(Integer, ForeignKey("benchmark_runs.id"), nullable=False)
    pricing_type = Column(String(40), nullable=False)
    currency = Column(String(10), nullable=False)
    effective_date = Column(String(40))
    normalization_version = Column(String(40))
    snapshot_json = Column(Text, nullable=False)
    cost_json = Column(Text)

    run = relationship("BenchmarkRun", back_populates="snapshots")


class SchemaMigration(Base):
    __tablename__ = "schema_migrations"
    version = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    applied_at = Column(String(40), nullable=False)
