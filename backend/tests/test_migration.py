"""The migration must build the schema the models describe — and undo it.

A migration that only applies forward is a one-way door. These tests run the
real Alembic migration against a scratch database and check three things:

1. it applies;
2. the resulting schema matches `Base.metadata` (no drift);
3. it downgrades cleanly enough that a second upgrade succeeds.

(3) catches the specific failure this project already hit once: dropping a
table does not drop the native enum types it used, so a downgrade leaves
orphan types behind and the next upgrade dies on "type already exists".
"""

from __future__ import annotations

import os

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, text

from academic_ai.infrastructure.db.base import Base
from tests._migration_sql import enum_type_names, migration_path

MIGRATION_DB = "academic_ai_migration_test"


def _urls() -> tuple[str, str]:
    base = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+psycopg://academic_ai_app:dev_only_not_a_secret"
        "@127.0.0.1:5434/academic_ai_test",
    )
    prefix = base.rsplit("/", 1)[0]
    return f"{prefix}/{MIGRATION_DB}", f"{prefix}/postgres"


@pytest.fixture(scope="module")
def migration_engine():
    url, admin_url = _urls()
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{MIGRATION_DB}"'))
    except Exception as exc:  # pragma: no cover - environment problem
        pytest.skip(f"PostgreSQL is not available: {exc}")
    finally:
        admin.dispose()

    engine = create_engine(url)
    yield engine
    engine.dispose()

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{MIGRATION_DB}" WITH (FORCE)'))
    admin.dispose()


def _alembic_config(url: str) -> Config:
    root = migration_path().parents[2]
    cfg = Config(str(root / "alembic.ini"))
    cfg.set_main_option("script_location", str(root / "alembic"))
    os.environ["DATABASE_URL"] = url
    return cfg


def _table_names(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        )
        return {r[0] for r in rows}


def _enum_names(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT t.typname FROM pg_type t "
                "JOIN pg_namespace n ON n.oid = t.typnamespace "
                "WHERE n.nspname = 'public' AND t.typtype = 'e'"
            )
        )
        return {r[0] for r in rows}


def test_migration_applies_and_matches_the_models(migration_engine) -> None:
    url, _ = _urls()
    cfg = _alembic_config(url)

    command.upgrade(cfg, "head")

    created = _table_names(migration_engine)
    expected = set(Base.metadata.tables) | {"alembic_version"}
    assert expected <= created, f"migration did not create: {expected - created}"

    # No drift: autogenerate against the migrated database must find nothing.
    with migration_engine.connect() as conn:
        context = MigrationContext.configure(
            conn, opts={"compare_type": True, "compare_server_default": True}
        )
        diff = compare_metadata(context, Base.metadata)
    assert diff == [], f"migration and models disagree: {diff}"


def test_migration_creates_the_integrity_triggers(migration_engine) -> None:
    with migration_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT tgname FROM pg_trigger WHERE NOT tgisinternal")
        )
        triggers = {r[0] for r in rows}

    assert {
        "trg_enrollment_snapshot_is_immutable",
        "trg_schedule_assignment_version_is_mutable",
        "trg_audit_log_is_append_only",
    } <= triggers


def test_migration_creates_the_exclusion_constraints(migration_engine) -> None:
    with migration_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT conname FROM pg_constraint WHERE contype = 'x'")
        )
        names = {r[0] for r in rows}

    assert names == {
        "ex_timeslot_no_overlap_within_day",
        "ex_schedule_assignment_no_room_double_booking",
    }


def test_downgrade_leaves_no_orphan_enum_types(migration_engine) -> None:
    """Then a second upgrade must succeed.

    Alembic does not emit DROP TYPE for native enums, so without the explicit
    cleanup in `downgrade()` this fails with "type already exists".
    """
    url, _ = _urls()
    cfg = _alembic_config(url)

    assert _enum_names(migration_engine) == set(enum_type_names())

    command.downgrade(cfg, "base")

    assert _enum_names(migration_engine) == set()
    assert _table_names(migration_engine) <= {"alembic_version"}

    command.upgrade(cfg, "head")
    assert set(Base.metadata.tables) <= _table_names(migration_engine)
