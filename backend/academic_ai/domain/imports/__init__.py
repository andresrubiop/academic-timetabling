"""CSV/XLSX imports (SESSION 15).

Framework-free: a spec describes an importable entity and validating a row
against one needs no database. Reading a file is infrastructure; resolving a
row's references is the application layer, because that is the only part that
needs to look anything up.
"""

from academic_ai.domain.imports.catalog import IMPORT_SPECS, spec_for
from academic_ai.domain.imports.diagnostics import (
    ImportDiagnostic,
    RowOutcome,
    RowPlan,
    Severity,
)
from academic_ai.domain.imports.spec import (
    ColumnSpec,
    ColumnType,
    ImportSpec,
    Lookup,
    coerce_value,
)

__all__ = [
    "IMPORT_SPECS",
    "ColumnSpec",
    "ColumnType",
    "ImportDiagnostic",
    "ImportSpec",
    "Lookup",
    "RowOutcome",
    "RowPlan",
    "Severity",
    "coerce_value",
    "spec_for",
]
