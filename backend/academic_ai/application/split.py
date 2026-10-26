"""Dividing a class that runs several hours into separate classes.

**Why dragging one hour cannot do this by itself.**

`ClassSession` is *the schedulable unit* — "one weekly meeting block"
(DATABASE.md §1). A class of three hours is **one** session that occupies
three contiguous slots, not three things that happen to sit together. Moving
one of its hours somewhere else is therefore not a move: it is a statement
that this is no longer one meeting but two, which changes what the institution
teaches rather than when it teaches it.

Making a drag do that silently would be the worst version of it. Somebody
nudging a block to tidy up a Wednesday would split a three-hour laboratory
into a one-hour and a two-hour class, and nothing on screen would say so —
the grid would look tidier and the course would have changed shape.

So it is an operation with a name, and the caller asks for it.

**Splitting a published class is refused.** `schedule_assignment` references
its session by `(id, duration_slots)`, a composite foreign key, so a session's
duration cannot change while an assignment names the old one — and a published
version's assignments are immutable. That is the schema saying what is true:
changing the shape of a published timetable is a new version of it, which is
what change requests exist for. The refusal says so rather than failing on a
constraint name.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from academic_ai.domain.enums import AuditAction, ScheduleVersionStatus
from academic_ai.security.rbac import Permission, Principal, authorise

__all__ = ["SplitError", "SplitResult", "split_session"]

log = logging.getLogger("academic_ai.split")


class SplitError(RuntimeError):
    """Refused, with a reason and, where there is one, the path to take."""


@dataclass(frozen=True, slots=True)
class SplitResult:
    original_session_id: str
    new_session_id: str
    original_slots: int
    new_slots: int


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError as exc:
        raise SplitError(f"{value!r} is not an id") from exc


def split_session(
    session,
    principal: Principal,
    class_session_id: uuid.UUID | str,
    *,
    first_part_slots: int,
) -> SplitResult:
    """Turn one session of N hours into two, of `first_part_slots` and the rest.

    The new session keeps the same section, type, delivery mode, room
    requirement, teachers and required features: it is the same teaching, said
    to happen in two meetings instead of one. Only the duration differs, and
    only because it had to be divided somewhere.
    """
    from academic_ai.infrastructure.db.models import (
        AuditLog,
        ClassSession,
        ScheduleAssignment,
        ScheduleVersion,
        SessionRequiredFeature,
        SessionTeacher,
    )

    # Structural: this changes what is taught, not when. `MANAGE_INSTITUTION`
    # would be too narrow — a coordinator plans their own programme's sessions
    # — but a view permission is far too wide.
    authorise(principal, Permission.EDIT_DRAFT_SCHEDULE)

    original = session.get(ClassSession, _as_uuid(class_session_id))
    if original is None:
        raise SplitError(f"no class session {class_session_id}")

    if original.duration_slots < 2:
        raise SplitError(
            "a class of one hour has nothing to divide; it is already a single "
            "meeting"
        )
    if not 1 <= first_part_slots < original.duration_slots:
        raise SplitError(
            f"the first part must be between 1 and {original.duration_slots - 1} "
            f"hour(s); {first_part_slots} would leave the second part empty"
        )

    placed_published = session.scalars(
        select(ScheduleAssignment)
        .join(
            ScheduleVersion,
            ScheduleVersion.id == ScheduleAssignment.schedule_version_id,
        )
        .where(ScheduleAssignment.class_session_id == original.id)
        .where(ScheduleVersion.status == ScheduleVersionStatus.PUBLISHED)
    ).first()
    if placed_published is not None:
        raise SplitError(
            "this class is in the published timetable, and the shape of a "
            "published timetable is only changed by publishing a new version. "
            "Propose it as a change request instead — dividing a class that "
            "students are already reading is not something to do in place."
        )

    second_part = original.duration_slots - first_part_slots

    # The new meeting, before the original shrinks: `schedule_assignment`
    # references a session by `(id, duration_slots)`, so shrinking first would
    # break any draft assignment still pointing at the old duration.
    highest = session.scalar(
        select(ClassSession.sequence_number)
        .where(ClassSession.section_id == original.section_id)
        .order_by(ClassSession.sequence_number.desc())
        .limit(1)
    )
    fresh = ClassSession(
        section_id=original.section_id,
        academic_period_id=original.academic_period_id,
        sequence_number=(highest or original.sequence_number) + 1,
        session_type=original.session_type,
        delivery_mode=original.delivery_mode,
        duration_slots=second_part,
        requires_timeslot=original.requires_timeslot,
        requires_room=original.requires_room,
        min_room_capacity=original.min_room_capacity,
        co_teaching_group_id=original.co_teaching_group_id,
        created_by_user_id=_as_uuid(principal.user_id),
    )
    session.add(fresh)
    session.flush()

    # The same teaching, in two meetings: whoever taught it still teaches both
    # halves, and whatever the room had to provide it still has to provide.
    for link in session.scalars(
        select(SessionTeacher).where(
            SessionTeacher.class_session_id == original.id
        )
    ):
        session.add(
            SessionTeacher(
                class_session_id=fresh.id,
                teacher_id=link.teacher_id,
                role=link.role,
            )
        )
    for feature in session.scalars(
        select(SessionRequiredFeature).where(
            SessionRequiredFeature.class_session_id == original.id
        )
    ):
        session.add(
            SessionRequiredFeature(
                class_session_id=fresh.id,
                room_feature_id=feature.room_feature_id,
                min_quantity=feature.min_quantity,
            )
        )

    # Any draft placement of the original is dropped: a class that used to run
    # three hours has no meaningful position now that it runs one, and leaving
    # the old assignment would claim a length the session no longer has.
    for assignment in session.scalars(
        select(ScheduleAssignment).where(
            ScheduleAssignment.class_session_id == original.id
        )
    ):
        session.delete(assignment)
    session.flush()

    original.duration_slots = first_part_slots
    original.updated_by_user_id = _as_uuid(principal.user_id)
    session.flush()

    session.add(
        AuditLog(
            actor_user_id=_as_uuid(principal.user_id),
            actor_description=f"{principal.role.value} via API",
            action=AuditAction.UPDATE,
            entity_type="class_session",
            entity_id=original.id,
            before={"duration_slots": first_part_slots + second_part},
            after={
                "duration_slots": first_part_slots,
                "new_session": str(fresh.id),
                "new_duration_slots": second_part,
            },
        )
    )
    log.info(
        "class session split",
        extra={"first_part": first_part_slots, "second_part": second_part},
    )
    return SplitResult(
        original_session_id=str(original.id),
        new_session_id=str(fresh.id),
        original_slots=first_part_slots,
        new_slots=second_part,
    )
