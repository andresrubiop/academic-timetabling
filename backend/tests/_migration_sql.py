"""Access to the trigger SQL defined by the baseline migration.

The test schema is built from ``Base.metadata``, which knows nothing about
triggers. Rather than copy the plpgsql into the test suite — where it would
drift from the migration within one session — the SQL is imported from the
migration module itself.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from types import ModuleType

_VERSIONS = pathlib.Path(__file__).resolve().parents[1] / "alembic" / "versions"

_MIGRATION = _VERSIONS / "13569e7ea0e8_session_1_academic_domain_baseline.py"
_CATALOG_MIGRATION = _VERSIONS / "b4734cedfe07_session_2_seed_constraint_type_catalog.py"


def _load_migration_module() -> ModuleType:
    """Import the migration file without needing an Alembic context.

    ``alembic.op`` is only touched inside the migration's functions, so
    importing the module to read its module-level SQL constants is safe.
    """
    name = "academic_ai_baseline_migration"
    if name in sys.modules:
        return sys.modules[name]

    spec = importlib.util.spec_from_file_location(name, _MIGRATION)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load migration module from {_MIGRATION}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def trigger_sql() -> str:
    """Every trigger the migrations install, concatenated.

    The metadata-built test schema has to carry all of them, not just
    SESSION 1's: a trigger added by a later migration and missing here would
    make its tests pass vacuously against a database that does not have it.
    """
    parts = [_load_migration_module()._TRIGGER_SQL]
    for path in sorted(_VERSIONS.glob("*.py")):
        if path == _MIGRATION:
            continue
        text = path.read_text()
        if "_TRIGGER_SQL" not in text:
            continue
        module = _load_named(path)
        extra = getattr(module, "_TRIGGER_SQL", None)
        if extra:
            parts.append(extra)
    return "\n".join(parts)


def _load_named(path: pathlib.Path):
    name = f"academic_ai_migration_{path.stem}"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise RuntimeError(f"cannot load migration from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def enum_type_names() -> tuple[str, ...]:
    return _load_migration_module()._ENUM_TYPE_NAMES


def migration_path() -> pathlib.Path:
    return _MIGRATION


def catalog_seed_rows() -> tuple[dict[str, object], ...]:
    """The frozen `constraint_type` rows the SESSION 2 migration inserts.

    Read from the migration rather than from the catalog on purpose: the whole
    value of the comparison is that these two are independent copies.
    """
    name = "academic_ai_catalog_seed_migration"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, _CATALOG_MIGRATION)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            raise RuntimeError(f"cannot load migration from {_CATALOG_MIGRATION}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]._SEED_ROWS
