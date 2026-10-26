"""The domain layer must stay framework-free.

ADR-0001 makes this a load-bearing rule rather than a preference: the
optimizer has to run outside the web process (local worker, remote worker,
HPC/Slurm, Kubernetes), and it can only do that if the domain it depends on
imports nothing from FastAPI or SQLAlchemy. That property is trivially easy
to lose to one convenient import, so it is tested.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

DOMAIN_ROOT = pathlib.Path(__file__).resolve().parents[1] / "academic_ai" / "domain"

FORBIDDEN_ROOTS = {
    "sqlalchemy",
    "fastapi",
    "alembic",
    "psycopg",
    "starlette",
    "pydantic",
    "redis",
    "ortools",
}


def _domain_modules() -> list[pathlib.Path]:
    return sorted(DOMAIN_ROOT.rglob("*.py"))


def test_domain_package_is_not_empty() -> None:
    assert _domain_modules(), "no domain modules found — check DOMAIN_ROOT"


@pytest.mark.parametrize("path", _domain_modules(), ids=lambda p: p.name)
def test_domain_module_imports_no_framework(path: pathlib.Path) -> None:
    tree = ast.parse(path.read_text(), filename=str(path))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])

    offenders = sorted(imported & FORBIDDEN_ROOTS)
    assert not offenders, (
        f"{path.relative_to(DOMAIN_ROOT.parents[1])} imports {offenders}; "
        "the domain layer must stay framework-free (ADR-0001)"
    )
