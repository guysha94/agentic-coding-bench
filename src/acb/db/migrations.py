"""Forward-only numbered migrations.

Deliberately small: a `schema_migrations` table plus an ordered list of steps. Migration 1
creates the schema from the SQLAlchemy metadata; later migrations are explicit SQL so the
history stays auditable and portable to PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, inspect, text

from acb.db.schema import Base


@dataclass
class Migration:
    version: int
    name: str
    apply: Callable[[Engine], None]


def _initial_schema(engine: Engine) -> None:
    Base.metadata.create_all(engine)


MIGRATIONS: list[Migration] = [
    Migration(1, "initial_schema", _initial_schema),
]


def _ensure_migrations_table(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, "
                "name VARCHAR(200) NOT NULL, "
                "applied_at VARCHAR(40) NOT NULL)"
            )
        )


def applied_versions(engine: Engine) -> set[int]:
    if not inspect(engine).has_table("schema_migrations"):
        return set()
    with engine.connect() as conn:
        return {row[0] for row in conn.execute(text("SELECT version FROM schema_migrations"))}


def migrate(engine: Engine) -> list[int]:
    """Apply pending migrations in order. Returns the versions applied."""
    _ensure_migrations_table(engine)
    done = applied_versions(engine)
    applied: list[int] = []
    for m in sorted(MIGRATIONS, key=lambda x: x.version):
        if m.version in done:
            continue
        m.apply(engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (version, name, applied_at) "
                    "VALUES (:v, :n, :t)"
                ),
                {"v": m.version, "n": m.name, "t": datetime.now(UTC).isoformat()},
            )
        applied.append(m.version)
    return applied


def current_version(engine: Engine) -> int:
    versions = applied_versions(engine)
    return max(versions) if versions else 0
