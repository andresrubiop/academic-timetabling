"""When a teacher or a room cannot be used.

The model has existed since SESSION 1, `TEACHER_AVAILABILITY` is a HARD
constraint in the catalog, and `load_snapshot` has read it into every problem
instance since SESSION 18. **Nothing has ever written it.** A coordinator
could not tell the platform that a lecturer does not teach on Friday
afternoons, which is the single most common thing anybody wants to say to a
timetabling system.

Absence means available
=======================

Only exceptions are stored: a row per teacher per slot would be the largest
table in the database for no benefit. That makes the *distinction between
"available" and "nobody has said"* worth keeping, and `has_declared_
availability` carries it — a teacher with no rows at all is reported as
undeclared, which is a diagnostic rather than a fact about their week.

So this writes UNAVAILABLE rows and deletes them. Setting a slot back to
available removes the row rather than storing `AVAILABLE`, because a stored
`AVAILABLE` and a missing row mean the same thing to the solver and only one
of them is a fact somebody asserted.

Replacing, not patching
=======================

The API takes the whole set of blocked slots for one owner in one period and
makes the stored rows match. A per-slot toggle endpoint would make an editor
that dropped one request leave a teacher blocked on an hour they had just
cleared, and neither side would know.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Iterable, Sequence

from sqlalchemy import select

from academic_ai.domain.enums import AuditAction, AvailabilityStatus
from academic_ai.security.rbac import Permission, Principal, authorise

__all__ = [
    "AvailabilityError",
    "OwnerAvailability",
    "load_availability",
    "set_availability",
]

log = logging.getLogger("academic_ai.availability")


class AvailabilityError(RuntimeError):
    """Refused, with a reason somebody editing a calendar can act on."""


@dataclass(frozen=True, slots=True)
class OwnerAvailability:
    owner_kind: str  # "teacher" | "room"
    owner_id: str
    #: Slots this owner cannot be used in.
    unavailable_slot_ids: tuple[str, ...]
    #: False when nobody has said anything at all. Different from "available
    #: every hour", and the analyser reports it as a diagnostic.
    has_declared: bool
    reason: str = ""


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError as exc:
        raise AvailabilityError(f"{value!r} is not an id") from exc


def _owner_column(kind: str):
    from academic_ai.infrastructure.db.models import Availability

    if kind == "teacher":
        return Availability.teacher_id
    if kind == "room":
        return Availability.room_id
    raise AvailabilityError(
        f"{kind!r} is not something availability is recorded for; expected "
        "'teacher' or 'room'"
    )


def load_availability(
    session,
    principal: Principal,
    *,
    owner_kind: str,
    owner_id: uuid.UUID | str,
    academic_period_id: uuid.UUID | str,
) -> OwnerAvailability:
    """The slots this owner cannot be used in, for one period."""
    from academic_ai.infrastructure.db.models import Availability, Timeslot

    authorise(principal, Permission.VIEW_PROGRAM_SCHEDULE)
    column = _owner_column(owner_kind)
    owner = _as_uuid(owner_id)

    rows = session.execute(
        select(Availability.timeslot_id, Availability.status, Availability.reason)
        .join(Timeslot, Timeslot.id == Availability.timeslot_id)
        .where(Timeslot.academic_period_id == _as_uuid(academic_period_id))
        .where(column == owner)
    ).all()

    blocked = [
        str(slot_id)
        for slot_id, status, _reason in rows
        if status is AvailabilityStatus.UNAVAILABLE
    ]
    reason = next((r for _s, _st, r in rows if r), "") or ""
    return OwnerAvailability(
        owner_kind=owner_kind,
        owner_id=str(owner),
        unavailable_slot_ids=tuple(sorted(blocked)),
        # Any row at all counts as having declared, including one that says
        # "available": somebody looked at this calendar and answered.
        has_declared=bool(rows),
        reason=reason,
    )


def set_availability(
    session,
    principal: Principal,
    *,
    owner_kind: str,
    owner_id: uuid.UUID | str,
    academic_period_id: uuid.UUID | str,
    unavailable_slot_ids: Iterable[str],
    reason: str = "",
) -> OwnerAvailability:
    """Make the stored rows match this set of blocked slots.

    Replacing rather than patching: an editor whose per-slot request went
    missing would otherwise leave somebody blocked on an hour they had just
    cleared, with neither side aware.
    """
    from academic_ai.infrastructure.db.models import (
        Availability,
        AuditLog,
        Timeslot,
    )

    # `MANAGE_CONSTRAINTS`, because that is what this is: availability feeds a
    # HARD constraint, and a wrong entry does not make a worse timetable — it
    # makes an infeasible one, or one that quietly excludes a teacher from
    # every hour they actually teach.
    authorise(principal, Permission.MANAGE_CONSTRAINTS)
    column = _owner_column(owner_kind)
    owner = _as_uuid(owner_id)
    period_id = _as_uuid(academic_period_id)

    valid = {
        str(t.id)
        for t in session.scalars(
            select(Timeslot).where(Timeslot.academic_period_id == period_id)
        )
    }
    requested = {str(s) for s in unavailable_slot_ids}
    unknown = requested - valid
    if unknown:
        # Refusing rather than ignoring: a slot id from another period means
        # the caller is editing the wrong week, and silently dropping it would
        # save an empty calendar and look like success.
        raise AvailabilityError(
            f"{len(unknown)} slot(s) do not belong to this period; the first "
            f"is {sorted(unknown)[0]}"
        )

    existing = {
        str(row.timeslot_id): row
        for row in session.scalars(
            select(Availability)
            .join(Timeslot, Timeslot.id == Availability.timeslot_id)
            .where(Timeslot.academic_period_id == period_id)
            .where(column == owner)
        )
    }

    for slot_id, row in existing.items():
        if slot_id not in requested:
            # Removed rather than set to AVAILABLE: a stored AVAILABLE and a
            # missing row mean the same thing to the solver, and only one of
            # them is a fact somebody asserted.
            session.delete(row)

    added = 0
    for slot_id in requested:
        row = existing.get(slot_id)
        if row is None:
            values = {
                "timeslot_id": _as_uuid(slot_id),
                "status": AvailabilityStatus.UNAVAILABLE,
                "reason": reason.strip()[:255] or None,
                "created_by_user_id": _as_uuid(principal.user_id),
            }
            values["teacher_id" if owner_kind == "teacher" else "room_id"] = owner
            session.add(Availability(**values))
            added += 1
        else:
            row.status = AvailabilityStatus.UNAVAILABLE
            row.reason = reason.strip()[:255] or None
            row.updated_by_user_id = _as_uuid(principal.user_id)
    session.flush()

    session.add(
        AuditLog(
            actor_user_id=_as_uuid(principal.user_id),
            actor_description=f"{principal.role.value} via API",
            action=AuditAction.UPDATE,
            entity_type=f"availability:{owner_kind}",
            entity_id=owner,
            before={"blocked": len(existing)},
            after={"blocked": len(requested), "period": str(period_id)},
        )
    )
    log.info(
        "availability set",
        extra={
            "owner_kind": owner_kind,
            "blocked": len(requested),
            "added": added,
        },
    )
    return load_availability(
        session,
        principal,
        owner_kind=owner_kind,
        owner_id=owner,
        academic_period_id=period_id,
    )
