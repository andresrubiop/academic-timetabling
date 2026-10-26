"""Row-level diagnostics: what is wrong, in which row, in which column.

The playbook's requirement is one sentence -- *never silently import malformed
rows* -- and the word doing the work is **silently**. Rejecting a
four-hundred-row spreadsheet because three rows are bad wastes the 397 that
were fine; importing the 397 without saying which three were dropped leaves
the registrar guessing. Neither is silent-free.

So every row gets a verdict, and the verdict names the row number the operator
sees in their spreadsheet -- 1-based, counting the header -- and the column
heading they typed, not the database column it maps to. An error that says
`department_id` about a file whose heading is `Departamento` is an error the
person holding the file cannot act on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

__all__ = [
    "ImportDiagnostic",
    "RowOutcome",
    "RowPlan",
    "Severity",
]


class Severity(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    #: The row cannot be imported as written.
    ERROR = "ERROR"
    #: The row will import, but something about it is worth seeing first --
    #: a value that was trimmed, a default that was applied.
    WARNING = "WARNING"


class RowOutcome(str, Enum):
    """What importing this row would do.

    `UNCHANGED` is the one that earns its place. Re-importing last week's
    spreadsheet should say "388 unchanged" rather than "388 updated": an
    operator who sees 388 updates on a file they did not edit has learned
    nothing about whether the import worked, and will stop reading the number.
    """

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    UNCHANGED = "UNCHANGED"
    #: Has an ERROR and will not be imported.
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class ImportDiagnostic:
    """One thing wrong with one row.

    ``column`` is the heading as it appears in the file, not the model
    attribute it maps to: an error naming `department_id` about a file whose
    heading is `Departamento` is one the person holding the file cannot act
    on.
    """

    #: 1-based, counting the header row, so it matches what the spreadsheet
    #: shows in its own row gutter.
    row_number: int
    severity: Severity
    message: str
    column: str | None = None
    #: The offending value, echoed back so the operator does not have to go
    #: hunting for the cell. Truncated -- a whole paragraph pasted into a code
    #: column should not make the report unreadable.
    value: str | None = None

    def __post_init__(self) -> None:
        if self.value is not None and len(self.value) > 120:
            object.__setattr__(self, "value", self.value[:117] + "...")

    @property
    def is_error(self) -> bool:
        return self.severity is Severity.ERROR

    def describe(self) -> str:
        where = f"row {self.row_number}"
        if self.column:
            where += f", column {self.column!r}"
        detail = f" (got {self.value!r})" if self.value is not None else ""
        return f"{where}: {self.message}{detail}"


@dataclass(frozen=True, slots=True)
class RowPlan:
    """One parsed row, what it would do, and what is wrong with it."""

    row_number: int
    outcome: RowOutcome
    #: The resolved values, ready to write. Empty when the row is REJECTED --
    #: a rejected row has nothing to write, and carrying half-resolved values
    #: invites somebody downstream to write them anyway.
    values: Mapping[str, object] = field(default_factory=dict)
    diagnostics: tuple[ImportDiagnostic, ...] = ()
    #: The natural key this row matched, when it matched one. What makes a
    #: second import of the same file an UPDATE rather than a duplicate.
    natural_key: tuple[str, ...] | None = None
    #: The primary key of the row this would update. None for CREATE.
    existing_id: str | None = None

    @property
    def is_rejected(self) -> bool:
        return self.outcome is RowOutcome.REJECTED

    @property
    def errors(self) -> tuple[ImportDiagnostic, ...]:
        return tuple(d for d in self.diagnostics if d.is_error)

    @property
    def warnings(self) -> tuple[ImportDiagnostic, ...]:
        return tuple(d for d in self.diagnostics if not d.is_error)
