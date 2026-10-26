"""The import pipeline: parse, resolve, validate, preview, confirm, apply.

The flow the playbook specifies, and one decision that is not obvious from it:

**Confirm re-derives the plan; it does not replay the preview.** A preview is
a photograph of what the import *would* do against the database as it was when
the operator looked. Between then and pressing confirm, somebody else may have
created the very room this file is about. Applying a stored plan would then
insert a duplicate that the preview promised was a creation. So the uploaded
file is kept, and confirm parses and validates it again from scratch. The
preview is advisory; the import is authoritative.

That also means an operator who waits an hour and confirms gets an honest
answer -- possibly a different one from the preview, reported as such --
rather than a stale one applied silently.

**Idempotency comes from natural keys**, not from remembering which files were
uploaded. Re-importing last week's spreadsheet matches every row to the row it
created and reports UNCHANGED. That is what makes "run the export again" a
safe thing for a registrar to do, which is the only way an import gets used
more than once.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.domain.imports import (
    ColumnType,
    ImportDiagnostic,
    ImportSpec,
    RowOutcome,
    RowPlan,
    Severity,
    coerce_value,
    spec_for,
)
from academic_ai.infrastructure.imports import ParseError, ParsedFile, parse_bytes

__all__ = [
    "ImportError_",
    "ImportPreview",
    "ImportResult",
    "OnError",
    "apply_plan",
    "plan_import",
]

log = logging.getLogger("academic_ai.imports")


class ImportError_(ValueError):
    """The import cannot proceed at all -- a bad file, a missing column."""


class OnError:
    """What to do with a file that has some bad rows.

    The default is `REJECT_FILE`, which is the conservative reading of "never
    silently import malformed rows": nothing is written unless everything is
    valid. `SKIP_ROWS` is available because a four-hundred-row spreadsheet
    with three bad rows is the normal case and rejecting all four hundred
    wastes the 397 -- but it has to be *chosen*, and the result reports every
    row it skipped.
    """

    REJECT_FILE = "REJECT_FILE"
    SKIP_ROWS = "SKIP_ROWS"


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """What this file would do. Writes nothing."""

    spec_code: str
    entity: str
    filename: str
    headings: tuple[str, ...]
    plans: tuple[RowPlan, ...]
    #: Problems with the file as a whole rather than with a row -- a missing
    #: required column, say. Reported before rows, because a file missing its
    #: `Código` column produces four hundred identical row errors otherwise
    #: and buries the one fact that matters.
    file_diagnostics: tuple[ImportDiagnostic, ...] = ()

    @property
    def creates(self) -> int:
        return sum(1 for p in self.plans if p.outcome is RowOutcome.CREATE)

    @property
    def updates(self) -> int:
        return sum(1 for p in self.plans if p.outcome is RowOutcome.UPDATE)

    @property
    def unchanged(self) -> int:
        return sum(1 for p in self.plans if p.outcome is RowOutcome.UNCHANGED)

    @property
    def rejected(self) -> int:
        return sum(1 for p in self.plans if p.is_rejected)

    @property
    def file_errors(self) -> tuple[ImportDiagnostic, ...]:
        return tuple(d for d in self.file_diagnostics if d.is_error)

    @property
    def is_importable(self) -> bool:
        """Whether anything can be written at all.

        False only for a file-level *error*: with no `Código` column there is
        no sense in which any row is importable. A warning -- an extra column
        the importer ignores -- must not block, or an export with one spare
        field becomes a file the operator has to edit before it is accepted.
        """
        return not self.file_errors

    def summary(self) -> str:
        if self.file_errors:
            return "the file cannot be read as " + self.entity
        parts = [f"{self.creates} new", f"{self.updates} updated"]
        if self.unchanged:
            parts.append(f"{self.unchanged} unchanged")
        if self.rejected:
            parts.append(f"{self.rejected} rejected")
        return ", ".join(parts)


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What was actually written."""

    preview: ImportPreview
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    committed: bool = False
    refusal: str | None = None

    def summary(self) -> str:
        if not self.committed:
            return self.refusal or "nothing was imported"
        parts = [f"{self.created} created", f"{self.updated} updated"]
        if self.unchanged:
            parts.append(f"{self.unchanged} unchanged")
        if self.skipped:
            parts.append(f"{self.skipped} skipped")
        return ", ".join(parts)


# ---------------------------------------------------------------------------
# Reference resolution
# ---------------------------------------------------------------------------


def _model_for(entity_table: str):
    from academic_ai.infrastructure.db import models as m

    by_table = {
        "department": m.Department,
        "program": m.Program,
        "course": m.Course,
        "campus": m.Campus,
        "building": m.Building,
        "room": m.Room,
        "teacher": m.Teacher,
        "student": m.Student,
        "academic period": m.AcademicPeriod,
    }
    model = by_table.get(entity_table)
    if model is None:  # pragma: no cover - specs are fixed
        raise ImportError_(f"no lookup target for {entity_table!r}")
    return model


def _build_lookup_index(
    session: Session, spec: ImportSpec, scope: Mapping[str, Any]
) -> dict[str, dict[str, dict[str, Any]]]:
    """Every code the file could reference, read once per column.

    One query per referenced table rather than one per row: a four-hundred-row
    enrolment file references at most a few hundred distinct students, and a
    per-row lookup turns an import into four hundred round trips for data that
    fits in a dictionary.
    """
    index: dict[str, dict[str, dict[str, Any]]] = {}
    for column in spec.columns:
        if column.type is not ColumnType.REFERENCE or column.lookup is None:
            continue
        model = _model_for(column.lookup.entity)
        match_column = getattr(model, column.lookup.match_on)
        carried = tuple(column.lookup.carry)
        stmt = select(match_column, model.id, *(getattr(model, c) for c in carried))
        # Scope the lookup where the target is scoped, so "MAT" resolves
        # within this institution rather than matching another one's.
        if hasattr(model, "institution_id") and scope.get("institution_id"):
            stmt = stmt.where(model.institution_id == scope["institution_id"])
        index[column.heading] = {
            str(row[0]): {"id": row[1], **dict(zip(carried, row[2:]))}
            for row in session.execute(stmt)
        }
    return index


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def _existing_by_natural_key(
    session: Session, spec: ImportSpec, scope: Mapping[str, Any]
) -> dict[tuple[str, ...], Any]:
    from academic_ai.infrastructure.db import models as m

    model = getattr(m, spec.entity)
    stmt = select(model)
    if hasattr(model, "institution_id") and scope.get("institution_id"):
        stmt = stmt.where(model.institution_id == scope["institution_id"])

    out: dict[tuple[str, ...], Any] = {}
    for row in session.scalars(stmt):
        key = tuple(
            "" if getattr(row, f, None) is None else str(getattr(row, f))
            for f in spec.natural_key
        )
        out[key] = row
    return out


def plan_import(
    session: Session,
    spec_code: str,
    data: bytes,
    filename: str,
    *,
    scope: Mapping[str, Any] | None = None,
) -> ImportPreview:
    """Work out what this file would do. Writes nothing.

    ``scope`` supplies what the file cannot -- the institution a teacher
    belongs to, say. It is taken from the request rather than from a column
    because an operator uploading their own institution's staff list should
    not have to repeat its code on every row, and letting a column say
    otherwise would let one institution's file write into another's data.
    """
    spec = spec_for(spec_code)
    scope = dict(scope or {})

    try:
        parsed: ParsedFile = parse_bytes(data, filename)
    except ParseError as exc:
        raise ImportError_(str(exc)) from exc

    missing = spec.missing_headings(parsed.headings)
    if missing:
        return ImportPreview(
            spec_code=spec.code,
            entity=spec.entity,
            filename=filename,
            headings=parsed.headings,
            plans=(),
            file_diagnostics=(
                ImportDiagnostic(
                    1,
                    Severity.ERROR,
                    "the file is missing required column(s): "
                    + ", ".join(missing),
                ),
            ),
        )

    unknown = [
        h for h in parsed.headings if h and spec.column_for(h) is None
    ]
    file_diagnostics: list[ImportDiagnostic] = [
        ImportDiagnostic(
            1,
            Severity.WARNING,
            # A warning, not an error: an export with an extra column the
            # importer does not use is normal, and refusing it would make the
            # operator edit a file that is otherwise fine.
            f"column {h!r} is not used by this import and will be ignored",
            h,
        )
        for h in unknown
    ]

    lookups = _build_lookup_index(session, spec, scope)
    existing = _existing_by_natural_key(session, spec, scope)

    plans: list[RowPlan] = []
    seen_keys: dict[tuple[str, ...], int] = {}

    for row_number, cells in parsed.rows:
        diagnostics: list[ImportDiagnostic] = []
        values: dict[str, Any] = dict(scope)

        for heading, raw in cells.items():
            column = spec.column_for(heading)
            if column is None:
                continue
            value, diagnostic = coerce_value(column, raw, row_number)
            if diagnostic is not None:
                diagnostics.append(diagnostic)
                continue
            if value is None:
                continue

            if column.type is ColumnType.REFERENCE and column.lookup is not None:
                resolved = lookups.get(column.heading, {}).get(str(value))
                if resolved is None:
                    diagnostics.append(
                        ImportDiagnostic(
                            row_number,
                            Severity.ERROR,
                            f"no {column.lookup.entity} with "
                            f"{column.lookup.match_on} {value!r} exists",
                            heading,
                            str(value),
                        )
                    )
                    continue
                values[column.lookup.target_field] = resolved["id"]
                for source, destination in column.lookup.carry.items():
                    values[destination] = resolved[source]
            else:
                values[column.field_name] = value

        missing_required = [
            column.heading
            for column in spec.columns
            if column.required
            and (
                column.lookup.target_field if column.lookup else column.field_name
            )
            not in values
            and not any(d.column == column.heading for d in diagnostics)
        ]
        for heading in missing_required:
            diagnostics.append(
                ImportDiagnostic(
                    row_number, Severity.ERROR, "this column is required", heading
                )
            )

        if any(d.is_error for d in diagnostics):
            plans.append(
                RowPlan(row_number, RowOutcome.REJECTED, {}, tuple(diagnostics))
            )
            continue

        key = tuple(
            "" if values.get(f) is None else str(values.get(f))
            for f in spec.natural_key
        )

        # Two rows in one file claiming the same identity is the operator's
        # mistake, and importing both would leave whichever came last silently
        # winning.
        if key in seen_keys:
            diagnostics.append(
                ImportDiagnostic(
                    row_number,
                    Severity.ERROR,
                    f"duplicates row {seen_keys[key]} in this same file "
                    f"({', '.join(spec.natural_key)} are the same)",
                )
            )
            plans.append(
                RowPlan(row_number, RowOutcome.REJECTED, {}, tuple(diagnostics))
            )
            continue
        seen_keys[key] = row_number

        current = existing.get(key)
        if current is None:
            values.update(spec.create_only_defaults)
            outcome = RowOutcome.CREATE
            existing_id = None
        else:
            existing_id = str(current.id)
            changed = {
                f: v
                for f, v in values.items()
                if f not in spec.create_only_defaults
                and getattr(current, f, None) != v
            }
            outcome = RowOutcome.UPDATE if changed else RowOutcome.UNCHANGED

        plans.append(
            RowPlan(
                row_number=row_number,
                outcome=outcome,
                values=values,
                diagnostics=tuple(diagnostics),
                natural_key=key,
                existing_id=existing_id,
            )
        )

    return ImportPreview(
        spec_code=spec.code,
        entity=spec.entity,
        filename=filename,
        headings=parsed.headings,
        plans=tuple(plans),
        file_diagnostics=tuple(file_diagnostics),
    )


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def apply_plan(
    session: Session,
    preview: ImportPreview,
    *,
    actor_user_id: str | None = None,
    on_error: str = OnError.REJECT_FILE,
) -> ImportResult:
    """Write the plan. Refuses rather than partially writing, unless told.

    The caller owns the transaction: this flushes but never commits, so a
    failure anywhere leaves nothing written.
    """
    from academic_ai.infrastructure.db import models as m

    if not preview.is_importable:
        return ImportResult(
            preview,
            refusal="; ".join(d.message for d in preview.file_errors),
        )

    if preview.rejected and on_error == OnError.REJECT_FILE:
        return ImportResult(
            preview,
            refusal=(
                f"{preview.rejected} of {len(preview.plans)} row(s) have errors "
                "and nothing was imported. Fix them, or re-submit asking to "
                "skip the bad rows."
            ),
        )

    model = getattr(m, preview.entity)
    spec = spec_for(preview.spec_code)
    actor = uuid.UUID(actor_user_id) if actor_user_id else None

    created = updated = unchanged = skipped = 0
    for plan in preview.plans:
        if plan.is_rejected:
            skipped += 1
            continue
        if plan.outcome is RowOutcome.UNCHANGED:
            unchanged += 1
            continue

        if plan.outcome is RowOutcome.CREATE:
            row = model(**plan.values)
            if hasattr(row, "created_by_user_id"):
                row.created_by_user_id = actor
            if hasattr(row, "updated_by_user_id"):
                row.updated_by_user_id = actor
            session.add(row)
            created += 1
        else:
            row = session.get(model, uuid.UUID(plan.existing_id))
            if row is None:  # pragma: no cover - re-planned immediately before
                skipped += 1
                continue
            for field_name, value in plan.values.items():
                # Never on update: an import must not silently reactivate a
                # room somebody retired, or reset a status a human set.
                if field_name in spec.create_only_defaults:
                    continue
                setattr(row, field_name, value)
            if hasattr(row, "updated_by_user_id"):
                row.updated_by_user_id = actor
            updated += 1

    session.flush()
    # `rows_created`, not `created`: `created` is a reserved LogRecord
    # attribute and passing it through `extra` raises KeyError inside the
    # logging call -- failing the import for a logging reason. SESSION 9 hit
    # the same thing with `message`; `tests/test_security.py` now scans for it.
    log.info(
        "import applied",
        extra={
            "spec": preview.spec_code,
            "rows_created": created,
            "rows_updated": updated,
            "rows_skipped": skipped,
        },
    )
    return ImportResult(
        preview,
        created=created,
        updated=updated,
        unchanged=unchanged,
        skipped=skipped,
        committed=True,
    )
