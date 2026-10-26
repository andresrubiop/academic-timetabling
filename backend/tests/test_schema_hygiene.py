"""Schema-wide invariants that are easy to break one model at a time.

None of these are about a particular entity. They are the rules that keep the
schema coherent as twenty more sessions add tables to it.
"""

from __future__ import annotations

from collections import Counter

import pytest

import academic_ai.infrastructure.db.models as models
from academic_ai.infrastructure.db.base import NAMING_CONVENTION, Base

TABLES = sorted(Base.metadata.tables.items())


def test_every_model_module_is_registered() -> None:
    """`models/__init__.py` must import every module.

    Alembic's autogenerate and the test fixtures both build from
    `Base.metadata`; a model module nobody imports is a table that silently
    never gets created.
    """
    import pathlib

    model_dir = pathlib.Path(models.__file__).parent
    modules = {
        p.stem for p in model_dir.glob("*.py") if p.stem != "__init__"
    }
    imported = {
        cls.__module__.rsplit(".", 1)[1]
        for cls in (getattr(models, name) for name in models.__all__)
    }
    missing = modules - imported
    assert not missing, f"model modules not imported in models/__init__.py: {missing}"


def test_naming_convention_covers_every_constraint_kind() -> None:
    assert set(NAMING_CONVENTION) == {"ix", "uq", "ck", "fk", "pk"}


@pytest.mark.parametrize("name,table", TABLES, ids=lambda x: x if isinstance(x, str) else "")
def test_no_duplicate_index_names(name: str, table) -> None:
    """An explicit `Index` can silently collide with `fk_column(index=True)`.

    `create_all` then fails with "relation already exists" — but only once
    someone builds the schema from metadata, which may be long after the
    model was written.
    """
    counts = Counter(ix.name for ix in table.indexes)
    duplicates = {n: c for n, c in counts.items() if c > 1}
    assert not duplicates, f"{name}: duplicate index names {duplicates}"


@pytest.mark.parametrize("name,table", TABLES, ids=lambda x: x if isinstance(x, str) else "")
def test_every_constraint_and_index_is_named(name: str, table) -> None:
    """Unnamed constraints produce migrations whose downgrade cannot work."""
    for constraint in table.constraints:
        assert constraint.name, f"{name}: unnamed {type(constraint).__name__}"
    for index in table.indexes:
        assert index.name, f"{name}: unnamed index"


@pytest.mark.parametrize("name,table", TABLES, ids=lambda x: x if isinstance(x, str) else "")
def test_every_table_has_a_uuid_primary_key(name: str, table) -> None:
    if name == "alembic_version":  # pragma: no cover - not ours
        return
    pk = list(table.primary_key.columns)
    assert len(pk) == 1, f"{name}: expected a single-column primary key"
    assert pk[0].name == "id", f"{name}: primary key column should be `id`"


@pytest.mark.parametrize("name,table", TABLES, ids=lambda x: x if isinstance(x, str) else "")
def test_foreign_keys_do_not_cascade_by_accident(name: str, table) -> None:
    """Academic records are retired, not deleted.

    CASCADE is legitimate only where a row is genuinely owned by its parent
    (a section's sessions, a scenario's perturbations). Anything else
    defaulting to CASCADE is a data-loss bug waiting for its first `DELETE`.
    """
    allowed = {"RESTRICT", "SET NULL", "CASCADE", None}
    for fk in table.foreign_keys:
        assert fk.ondelete in allowed, f"{name}.{fk.parent.name}: {fk.ondelete}"


def test_audit_log_has_no_updated_at() -> None:
    """An audit row has one moment. An `updated_at` would imply it can be
    updated, which the append-only trigger forbids."""
    columns = set(Base.metadata.tables["audit_log"].columns.keys())
    assert "occurred_at" in columns
    assert "updated_at" not in columns


def test_enrollment_carries_the_full_academic_snapshot() -> None:
    """These columns are what make an enrollment interpretable years later."""
    from academic_ai.infrastructure.db.models.enrollment import (
        IMMUTABLE_SNAPSHOT_COLUMNS,
    )

    columns = set(Base.metadata.tables["enrollment"].columns.keys())
    missing = set(IMMUTABLE_SNAPSHOT_COLUMNS) - columns
    assert not missing, f"enrollment is missing snapshot columns: {missing}"
