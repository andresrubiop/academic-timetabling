"""What an import target is: columns, natural key, and how references resolve.

The design decision that shapes everything else: **spreadsheets carry natural
keys, not UUIDs.** A registrar's file says `MAT` in a `Departamento` column;
it does not say `9b5fcf97-9526-4a7e-8cd5-b4e8de7b713f`, and asking for one
would mean the import can only be produced by a system that already has the
data. So every foreign key in an import spec is declared as a *lookup* --
which table, matched on which column -- and an unresolvable value becomes a
row-level error naming the code the operator actually typed.

The same choice makes idempotency possible. A row's natural key is what says
"this is the same teacher as last week", so re-importing an unchanged file
reports UNCHANGED rather than creating four hundred duplicates.

Framework-free: a spec is a description, and validating a row against one
needs no database. Resolving the lookups does, which is why that lives in the
application layer and takes the resolved values as an argument here.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Mapping, Sequence

from academic_ai.domain.imports.diagnostics import (
    ImportDiagnostic,
    Severity,
)

__all__ = [
    "ColumnSpec",
    "ColumnType",
    "ImportSpec",
    "Lookup",
    "coerce_value",
]


class ColumnType(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    TEXT = "TEXT"
    INTEGER = "INTEGER"
    BOOLEAN = "BOOLEAN"
    DATE = "DATE"
    ENUM = "ENUM"
    #: Resolved against another table by its natural key. See `Lookup`.
    REFERENCE = "REFERENCE"


@dataclass(frozen=True, slots=True)
class Lookup:
    """A reference resolved by natural key rather than by id.

    ``entity`` names the target for the *message*, not for dispatch -- "no
    department with code 'MAT'" is what the operator needs, and it should not
    depend on the caller remembering to phrase it.
    """

    entity: str
    #: The model attribute this resolves into, e.g. `department_id`.
    target_field: str
    #: The column on the target matched against the cell, e.g. `code`.
    match_on: str = "code"
    #: Fields copied from the row this resolves to, as {source: destination}.
    #:
    #: Some foreign keys are *derived* rather than typed. A room's `campus_id`
    #: is NOT NULL and is bound to its building's campus by a composite
    #: foreign key (SESSION 1), so a file that names the building has already
    #: said which campus -- asking for it again would let an operator state
    #: two things that contradict each other, and the database would reject
    #: the row for a reason the file gives no hint about.
    carry: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ColumnSpec:
    """One column of an import file."""

    #: The heading as it appears in the file. Matched case-insensitively and
    #: ignoring surrounding whitespace, because a spreadsheet round-tripped
    #: through three people has ` Código ` in it at least once.
    heading: str
    #: The model attribute it writes to. For a REFERENCE this is unused --
    #: `Lookup.target_field` says where the resolved id goes.
    field_name: str
    type: ColumnType = ColumnType.TEXT
    required: bool = False
    lookup: Lookup | None = None
    #: For ENUM columns: the permitted values, so an invalid one can list them.
    choices: tuple[str, ...] = ()
    #: Applied after coercion. Raises ValueError with a message the operator
    #: can act on.
    validate: Callable[[object], None] | None = None
    #: Alternative headings accepted for the same column -- a file in Spanish
    #: and one in English should both import without anybody renaming columns.
    aliases: tuple[str, ...] = ()

    def matches(self, heading: str) -> bool:
        candidate = heading.strip().casefold()
        return candidate == self.heading.strip().casefold() or any(
            candidate == alias.strip().casefold() for alias in self.aliases
        )


@dataclass(frozen=True, slots=True)
class ImportSpec:
    """One importable entity."""

    code: str
    entity: str
    columns: tuple[ColumnSpec, ...]
    #: Fields whose combination identifies a row across imports. Almost always
    #: a code plus whatever scopes it -- a section code means nothing without
    #: the offering it belongs to.
    natural_key: tuple[str, ...]
    #: Values written on create and never on update. `status` belongs here:
    #: an import should not silently reactivate a room somebody retired.
    create_only_defaults: Mapping[str, object] = field(default_factory=dict)
    description: str = ""

    def column_for(self, heading: str) -> ColumnSpec | None:
        for column in self.columns:
            if column.matches(heading):
                return column
        return None

    @property
    def required_headings(self) -> tuple[str, ...]:
        return tuple(c.heading for c in self.columns if c.required)

    def missing_headings(self, headings: Sequence[str]) -> tuple[str, ...]:
        """Required columns the file does not have.

        Reported before any row is read: a file missing its `Código` column
        produces four hundred identical row errors otherwise, which buries the
        one fact the operator needs.
        """
        present = [h for h in headings if self.column_for(h) is not None]
        return tuple(
            column.heading
            for column in self.columns
            if column.required
            and not any(column.matches(h) for h in present)
        )


_TRUE = {"true", "1", "yes", "y", "si", "sí", "verdadero", "x"}
_FALSE = {"false", "0", "no", "n", "falso", ""}

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d")


def coerce_value(
    column: ColumnSpec, raw: object, row_number: int
) -> tuple[object | None, ImportDiagnostic | None]:
    """A cell, as the type the column declares -- or a diagnostic saying why not.

    Returns `(value, None)` on success and `(None, diagnostic)` on failure, so
    a caller cannot accidentally use a value that failed to parse: there is
    never a value *and* an error.
    """
    text = "" if raw is None else str(raw).strip()

    if not text:
        if column.required:
            return None, ImportDiagnostic(
                row_number,
                Severity.ERROR,
                "this column is required and the cell is empty",
                column.heading,
            )
        return None, None

    if column.type in (ColumnType.TEXT, ColumnType.REFERENCE):
        value: object = text

    elif column.type is ColumnType.INTEGER:
        try:
            # Spreadsheets hand back 30.0 for a cell showing 30, so an int()
            # straight off the string fails on data that is perfectly fine.
            value = int(float(text))
        except ValueError:
            return None, ImportDiagnostic(
                row_number, Severity.ERROR, "expected a whole number",
                column.heading, text,
            )

    elif column.type is ColumnType.BOOLEAN:
        lowered = text.casefold()
        if lowered in _TRUE:
            value = True
        elif lowered in _FALSE:
            value = False
        else:
            return None, ImportDiagnostic(
                row_number,
                Severity.ERROR,
                "expected yes/no (also accepts true/false, 1/0, sí/no)",
                column.heading,
                text,
            )

    elif column.type is ColumnType.DATE:
        parsed = None
        if isinstance(raw, dt.datetime):
            parsed = raw.date()
        elif isinstance(raw, dt.date):
            parsed = raw
        else:
            for fmt in _DATE_FORMATS:
                try:
                    parsed = dt.datetime.strptime(text, fmt).date()
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None, ImportDiagnostic(
                row_number,
                Severity.ERROR,
                "expected a date (YYYY-MM-DD or DD/MM/YYYY)",
                column.heading,
                text,
            )
        value = parsed

    elif column.type is ColumnType.ENUM:
        upper = text.upper().replace(" ", "_")
        if upper not in column.choices:
            return None, ImportDiagnostic(
                row_number,
                Severity.ERROR,
                "not one of the accepted values: " + ", ".join(column.choices),
                column.heading,
                text,
            )
        value = upper

    else:  # pragma: no cover - the enum is exhaustive
        value = text

    if column.validate is not None:
        try:
            column.validate(value)
        except ValueError as exc:
            return None, ImportDiagnostic(
                row_number, Severity.ERROR, str(exc), column.heading, text
            )

    return value, None
