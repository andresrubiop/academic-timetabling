"""Applying an approved change to the published timetable.

The last place where this platform's story stopped short of what it promises.
A `change_request` could be proposed, measured, routed to the right people and
approved — and the timetable never moved. Every session since SESSION 12
recorded this as deliberate and escalated, because the question is not how to
write the rows. It is **who may apply, and when.**

Who: whoever holds `PUBLISH_SCHEDULE`, and not merely whoever approved.
====================================================================

An approval is *permission* to apply, not the application. Collapsing the two
would mean the last approver's click silently republishes the institution's
timetable — which is a different act, performed at a different moment, usually
by a different person, and the one that students actually read. The chain says
"this may happen"; somebody then makes it happen, and their name is on it.

It also keeps the two failure modes apart. An approval that turns out to be
wrong is withdrawn before anybody applies it. A publication that turns out to
be wrong is a new version.

When: only while the timetable it was measured against is still the one on
=======================================================================
the wall.

Every request records `base_schedule_version_id` — the version its impact was
computed from. If the published version has moved on, that impact described a
timetable that no longer exists: the affected students, the new conflicts, the
per-tier deltas the approvers read were all measured somewhere else. Applying
anyway would publish a schedule nobody approved, and it would look exactly
like one they did.

So a stale approval is **refused**, not merged. There is no safe automatic
rebase here: re-measuring produces a different impact, and a different impact
may well need a different chain (`assess_scope`). Re-analysing is a decision,
and it belongs to the person who proposed the change.

What it writes: a new version, never an edit
============================================

The database refuses to alter a published version's assignments, and that is
the rule rather than an obstacle. Applying builds a new version from the base,
publishes it, and supersedes the old one — so the timetable students read
yesterday is still readable, and `parent_version_id` says what it became.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select

from academic_ai.domain.enums import (
    AuditAction,
    ChangeRequestStatus,
    ScenarioStatus,
    ScheduleVersionStatus,
)
from academic_ai.security.rbac import Permission, Principal, authorise

__all__ = ["ApplyError", "AppliedChange", "apply_change_request"]

log = logging.getLogger("academic_ai.apply")


class ApplyError(RuntimeError):
    """Refused, with a reason the person holding the request can act on."""


@dataclass(frozen=True, slots=True)
class AppliedChange:
    change_request_id: str
    code: str
    schedule_version_id: str
    version_number: int
    superseded_version_id: str
    moved_sessions: int


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError as exc:
        raise ApplyError(f"{value!r} is not an id") from exc


def apply_change_request(
    session,
    principal: Principal,
    request_id: uuid.UUID | str,
    *,
    now: dt.datetime | None = None,
) -> AppliedChange:
    """Publish a new timetable that includes an approved change."""
    from academic_ai.infrastructure.db.models import (
        AuditLog,
        ChangeRequest,
        Scenario,
        ScenarioPerturbation,
        ScheduleAssignment,
        ScheduleVersion,
    )

    authorise(principal, Permission.PUBLISH_SCHEDULE)
    now = now or dt.datetime.now(dt.timezone.utc)

    request = session.get(ChangeRequest, _as_uuid(request_id))
    if request is None:
        raise ApplyError(f"no change request {request_id}")

    if request.status is ChangeRequestStatus.APPLIED:
        # Idempotent by refusal rather than by silence: "already applied" is
        # something the caller needs to know, and a second new version for one
        # approval would be two publications from one decision.
        raise ApplyError(
            f"{request.code} was already applied as schedule version "
            f"{request.applied_schedule_version_id}"
        )
    if request.status is not ChangeRequestStatus.APPROVED:
        raise ApplyError(
            f"{request.code} is {request.status.value}; only an APPROVED "
            "request may be applied"
        )

    base = session.get(ScheduleVersion, request.base_schedule_version_id)
    if base is None:  # pragma: no cover - a foreign key guarantees it
        raise ApplyError(f"{request.code} names a base version that is gone")

    published = session.scalars(
        select(ScheduleVersion)
        .where(ScheduleVersion.academic_period_id == request.academic_period_id)
        .where(ScheduleVersion.status == ScheduleVersionStatus.PUBLISHED)
    ).first()

    if published is None:
        raise ApplyError(
            f"{request.code} cannot be applied: this period has no published "
            "timetable to change"
        )
    if published.id != base.id:
        # The heart of it. See the module docstring: the impact everybody
        # approved was measured against a timetable that is no longer on the
        # wall, and re-measuring is a decision rather than a merge.
        raise ApplyError(
            f"{request.code} was measured against schedule version "
            f"{base.version_number}, and the published timetable is now "
            f"{published.version_number}. Its impact describes a timetable "
            "that no longer exists. Re-analyse the change and submit it "
            "again — the numbers the approvers read would not be the numbers "
            "they would get."
        )

    moves = list(
        session.scalars(
            select(ScenarioPerturbation)
            .where(ScenarioPerturbation.scenario_id == request.scenario_id)
            .order_by(ScenarioPerturbation.sequence_number)
        )
    )
    if not moves:
        raise ApplyError(f"{request.code} carries no moves to apply")

    by_session = {m.class_session_id: m for m in moves}
    moved = 0
    # Two passes on purpose. The first works out what the new timetable would
    # be; the second writes it. Interleaving them would let SQLAlchemy's
    # autoflush hit the database's own exclusion constraint before the check
    # below runs, and the caller would get "internal error" for a decision
    # that was theirs to reconsider.
    planned: list[tuple[uuid.UUID, uuid.UUID | None, uuid.UUID, int, object]] = []

    for assignment in session.scalars(
        select(ScheduleAssignment).where(
            ScheduleAssignment.schedule_version_id == base.id
        )
    ):
        move = by_session.get(assignment.class_session_id)
        start_timeslot_id = assignment.start_timeslot_id
        room_id = assignment.room_id
        if move is not None:
            start_timeslot_id = move.timeslot_id or start_timeslot_id
            # `room_id` absent on the perturbation means "leave the room
            # alone" — the timetable grid moves along the time axis, and a
            # move that silently unassigned a room would be a different change
            # from the one that was approved.
            room_id = move.room_id if move.room_id is not None else room_id
            moved += 1
        planned.append(
            (
                assignment.class_session_id,
                room_id,
                start_timeslot_id,
                assignment.duration_slots,
                assignment,
            )
        )

    _refuse_collisions(session, [(a, b, c, d) for a, b, c, d, _ in planned])

    if moved != len(moves):
        # A move naming a session the base version never placed. Refusing
        # rather than dropping it: the approved change and the published one
        # would otherwise differ by exactly the part nobody noticed.
        raise ApplyError(
            f"{request.code} moves {len(moves)} session(s), but only {moved} "
            "of them are placed in the published timetable"
        )

    # --- the new version ---------------------------------------------------
    # Created only once every refusal above has passed. Building it first and
    # validating afterwards left a half-made version behind whenever an apply
    # was refused — harmless while the request's transaction rolls back, and
    # exactly the kind of thing that stops being harmless when somebody calls
    # this from a script that does not.
    highest = session.scalar(
        select(ScheduleVersion.version_number)
        .where(ScheduleVersion.schedule_id == base.schedule_id)
        .order_by(ScheduleVersion.version_number.desc())
        .limit(1)
    )
    fresh = ScheduleVersion(
        schedule_id=base.schedule_id,
        institution_id=base.institution_id,
        academic_period_id=base.academic_period_id,
        version_number=(highest or base.version_number) + 1,
        parent_version_id=base.id,
        # DRAFT while its assignments are written: the database refuses to add
        # them to anything else, which is the same rule that makes a published
        # timetable trustworthy.
        status=ScheduleVersionStatus.DRAFT,
        notes=f"Aplicación de {request.code}: {request.title}",
        created_by_user_id=_as_uuid(principal.user_id),
    )
    session.add(fresh)
    session.flush()

    for class_session_id, room_id, start_timeslot_id, duration, source in planned:
        slot = _slot(session, start_timeslot_id)
        session.add(
            ScheduleAssignment(
                schedule_version_id=fresh.id,
                class_session_id=class_session_id,
                academic_period_id=source.academic_period_id,
                start_timeslot_id=start_timeslot_id,
                day_of_week=slot.day_of_week,
                start_slot_index=slot.day_slot_index,
                duration_slots=duration,
                requires_room=source.requires_room,
                room_id=room_id,
                is_locked=source.is_locked,
                created_by_user_id=_as_uuid(principal.user_id),
            )
        )
    session.flush()

    # --- swap the published version ---------------------------------------
    # In this order, because only one version per period may be PUBLISHED.
    published.status = ScheduleVersionStatus.SUPERSEDED
    session.flush()

    fresh.status = ScheduleVersionStatus.PUBLISHED
    fresh.published_at = now
    fresh.published_by_user_id = _as_uuid(principal.user_id)
    session.flush()

    request.status = ChangeRequestStatus.APPLIED
    request.applied_schedule_version_id = fresh.id
    request.updated_by_user_id = _as_uuid(principal.user_id)

    scenario = session.get(Scenario, request.scenario_id)
    if scenario is not None:
        # PROMOTED at last: the timetable does now reflect it, which is the
        # one thing that status has always meant.
        scenario.status = ScenarioStatus.PROMOTED

    session.add(
        AuditLog(
            actor_user_id=_as_uuid(principal.user_id),
            actor_description=f"{principal.role.value} via API",
            action=AuditAction.PUBLISH,
            entity_type="change_request",
            entity_id=request.id,
            before={"schedule_version": str(base.id)},
            after={
                "schedule_version": str(fresh.id),
                "version_number": fresh.version_number,
                "moved_sessions": moved,
            },
        )
    )
    session.flush()

    log.info(
        "change request applied",
        extra={
            "change_request": request.code,
            "version_number": fresh.version_number,
            "moved_sessions": moved,
        },
    )
    return AppliedChange(
        change_request_id=str(request.id),
        code=request.code,
        schedule_version_id=str(fresh.id),
        version_number=fresh.version_number,
        superseded_version_id=str(published.id),
        moved_sessions=moved,
    )


def _refuse_collisions(session, planned) -> None:
    """Refuse a change that would double-book a room or a teacher.

    The database already refuses this — `ex_schedule_assignment_no_room_double
    _booking` is a GiST exclusion constraint — and that is the wrong place for
    it to surface. It fires mid-write as a 500 with no indication of *which*
    two classes collide, and the person applying is left with "internal
    error" for a decision that was theirs to reconsider.

    It is also not a bug in the proposal. `compute_domains` answers where a
    session *may* go given its own constraints; whether another class is
    already in that room at that hour is a property of the whole placement,
    which is what the impact preview's `occupancy_clashes` warns about and
    deliberately does not block — CP-SAT is the feasibility authority, not the
    grid, and a coordinator may legitimately propose something that needs the
    other class moved too. What must not happen is publishing it.

    Teacher collisions are checked here as well, though no database constraint
    covers them: a teacher in two rooms at once is exactly as impossible as a
    room holding two classes, and only one of the two was being caught.
    """
    from academic_ai.infrastructure.db.models import SessionTeacher, Timeslot

    slots = {
        t.id: t
        for t in session.scalars(
            select(Timeslot).where(
                Timeslot.id.in_({start for _s, _r, start, _d in planned})
            )
        )
    }

    def covered(start_id, duration: int) -> set[tuple[str, int]]:
        """The (day, index) cells a placement occupies."""
        slot = slots.get(start_id)
        if slot is None:
            return set()
        return {
            (slot.day_of_week.value, slot.day_slot_index + step)
            for step in range(max(1, duration))
        }

    rooms: dict[tuple[uuid.UUID, str, int], uuid.UUID] = {}
    for session_id, room_id, start_id, duration in planned:
        if room_id is None:
            continue
        for day, index in covered(start_id, duration):
            key = (room_id, day, index)
            other = rooms.get(key)
            if other is not None and other != session_id:
                raise ApplyError(
                    f"applying this would put two classes in the same room at "
                    f"{day} slot {index}. The proposal was legal for the "
                    "session that moved; it collides with what is already "
                    "there, and the other class would have to move too."
                )
            rooms[key] = session_id

    teachers_of: dict[uuid.UUID, list[uuid.UUID]] = {}
    for class_session_id, teacher_id in session.execute(
        select(SessionTeacher.class_session_id, SessionTeacher.teacher_id).where(
            SessionTeacher.class_session_id.in_({s for s, _r, _t, _d in planned})
        )
    ):
        teachers_of.setdefault(class_session_id, []).append(teacher_id)

    busy: dict[tuple[uuid.UUID, str, int], uuid.UUID] = {}
    for session_id, _room_id, start_id, duration in planned:
        for teacher_id in teachers_of.get(session_id, ()):
            for day, index in covered(start_id, duration):
                key = (teacher_id, day, index)
                other = busy.get(key)
                if other is not None and other != session_id:
                    raise ApplyError(
                        f"applying this would put one teacher in two classes "
                        f"at {day} slot {index}."
                    )
                busy[key] = session_id


def _slot(session, timeslot_id):
    from academic_ai.infrastructure.db.models import Timeslot

    slot = session.get(Timeslot, timeslot_id)
    if slot is None:
        raise ApplyError(f"timeslot {timeslot_id} does not exist")
    return slot
