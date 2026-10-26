"""Variable domains, computed before any solver sees the instance.

`variables` states the rule; this module applies it. Every constraint that
depends on **one session alone** is enforced by shrinking that session's
variable domain rather than by posting a constraint:

===============================  ==========================================
constraint                       becomes
===============================  ==========================================
SESSION_WITHIN_DAY_GRID          starts where the session fits in the day
SESSION_SLOTS_SCHEDULABLE        starts whose whole span is schedulable
TEACHER_AVAILABILITY             starts where every teacher is free
ROOM_ACTIVE                      rooms that are active
ROOM_CAPACITY                    rooms with enough seats
ROOM_CAMPUS_MATCH                rooms on the offering's campus
ROOM_FEATURE_REQUIREMENT         rooms carrying the required features
ROOM_TYPE_ALLOWED                rooms on the session's allowed list
===============================  ==========================================

The reason is diagnostic, not performance. An empty domain is a **provable,
localised infeasibility that names one session**: "MAT101-A lecture cannot be
placed — it needs 3 consecutive schedulable slots and Tuesday is the only day
with 3, but its teacher is unavailable then." The identical fact discovered
inside CP-SAT is a bare INFEASIBLE for the entire institution, which is the
answer SESSION 6 exists to prevent anyone ever having to act on.

Restriction is conditional on the constraint still being HARD. A room-capacity
limit a named human downgraded must *not* prune the domain — it has become an
objective term, and pruning it would enforce something the institution
explicitly decided to stop enforcing. `_is_hard` is the only place that
decision is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from academic_ai.domain.enums import ConstraintCategory, DayOfWeek
from academic_ai.domain.scheduling.diagnostics import Diagnostic, DiagnosticCode
from academic_ai.domain.scheduling.problem import (
    BaseSnapshot,
    EffectiveConstraint,
    RoomSpec,
    SessionSpec,
)
from academic_ai.domain.scheduling.variables import DaySlot, feasible_start_indices

__all__ = [
    "SessionDomain",
    "compute_domains",
    "feasible_rooms_for",
    "feasible_starts_for",
]


def _is_hard(constraints: Mapping[str, EffectiveConstraint], code: str) -> bool:
    """Whether ``code`` is still HARD *everywhere*, and so safe to prune on.

    Only an **institution-wide** downgrade stops the pruning. A downgrade
    scoped to one room or one section says nothing about the rest of the
    institution, and treating it as if it did was a real defect: a capacity
    override attached to a single auditorium removed `ROOM_CAPACITY` from the
    enforced set entirely, unpruned every room for every session, and — since
    the validator reads the same set — went unreported.

    Domain pruning is all-or-nothing by nature: it happens once per session,
    before any room is chosen. Honouring a scoped downgrade here would mean
    pruning per (session, candidate room) with a `ScopeContext`, which is the
    proper fix and is recorded as owed. Until then the conservative reading is
    the safe one — keep enforcing, and let the scoped override express itself
    in the objective rather than by silently deleting a HARD constraint.
    """
    for constraint in constraints.get(code, ()):
        if not constraint.scope.is_institution_wide:
            continue
        if not constraint.is_active:
            return False
        return constraint.effective_category is ConstraintCategory.HARD
    return True


@dataclass(frozen=True, slots=True)
class SessionDomain:
    """The values one session's variables may still take.

    ``room_slot_ids`` is empty and meaningless for a session with
    ``requires_room = False``; use `needs_room` rather than testing emptiness,
    because "no room needed" and "no room possible" are the two facts this
    whole module exists to keep apart.
    """

    session_id: str
    start_slot_ids: tuple[str, ...]
    room_ids: tuple[str, ...]
    needs_room: bool

    @property
    def is_empty(self) -> bool:
        """True when this session cannot be placed at all."""
        if not self.start_slot_ids:
            return True
        return self.needs_room and not self.room_ids

    @property
    def size(self) -> int:
        """Cardinality of the session's search space."""
        rooms = len(self.room_ids) if self.needs_room else 1
        return len(self.start_slot_ids) * rooms


def feasible_starts_for(
    session: SessionSpec,
    snapshot: BaseSnapshot,
    constraints: Mapping[str, EffectiveConstraint] | None = None,
) -> tuple[str, ...]:
    """Slot ids at which ``session`` may start.

    A start survives when the whole span fits inside one day on schedulable
    slots, and — while TEACHER_AVAILABILITY is HARD — when none of the
    session's teachers is unavailable for any slot of that span.
    """
    constraints = constraints or {}
    if not session.requires_timeslot:
        return ()

    blocked_slot_ids: set[str] = set()
    if _is_hard(constraints, "TEACHER_AVAILABILITY"):
        by_teacher = {t.id: t for t in snapshot.teachers}
        for teacher_id in session.teacher_ids:
            teacher = by_teacher.get(teacher_id)
            if teacher is not None:
                blocked_slot_ids |= teacher.unavailable_slot_ids

    starts: list[str] = []
    for day in snapshot.period.days:
        day_slots = snapshot.slots_by_day(day)
        if not day_slots:
            continue
        by_index = {s.in_day_index: s for s in day_slots}
        candidates = feasible_start_indices(
            tuple(
                DaySlot(s.in_day_index, s.global_index, s.is_schedulable)
                for s in day_slots
            ),
            session.duration_slots,
        )
        for index in candidates:
            span = [by_index[i] for i in range(index, index + session.duration_slots)]
            if any(slot.id in blocked_slot_ids for slot in span):
                continue
            starts.append(by_index[index].id)
    return tuple(starts)


def _has_required_features(session: SessionSpec, room: RoomSpec) -> bool:
    return all(
        room.features.get(feature_id, 0) >= needed
        for feature_id, needed in session.required_features.items()
    )


def feasible_rooms_for(
    session: SessionSpec,
    snapshot: BaseSnapshot,
    constraints: Mapping[str, EffectiveConstraint] | None = None,
    *,
    seat_demand: int | None = None,
) -> tuple[str, ...]:
    """Room ids ``session`` may occupy.

    ``seat_demand`` overrides the session's own figure so a co-teaching group
    can pass the summed demand of all its members — a merged offering needs
    one room big enough for everybody, and pruning each member against its own
    section size would leave rooms in the domain that cannot hold the group
    (ADR-0004).
    """
    constraints = constraints or {}
    if not session.requires_room:
        return ()

    demand = session.seat_demand if seat_demand is None else seat_demand
    enforce_capacity = _is_hard(constraints, "ROOM_CAPACITY")
    enforce_campus = _is_hard(constraints, "ROOM_CAMPUS_MATCH")
    enforce_active = _is_hard(constraints, "ROOM_ACTIVE")
    enforce_features = _is_hard(constraints, "ROOM_FEATURE_REQUIREMENT")
    enforce_allowed = _is_hard(constraints, "ROOM_TYPE_ALLOWED")

    rooms: list[str] = []
    for room in snapshot.rooms:
        if enforce_active and not room.is_active:
            continue
        if enforce_campus and room.campus_id != session.campus_id:
            continue
        if enforce_capacity and room.capacity < demand:
            continue
        if enforce_features and not _has_required_features(session, room):
            continue
        # An empty allow-list means unrestricted, never "nothing allowed".
        if (
            enforce_allowed
            and session.allowed_room_ids
            and room.id not in session.allowed_room_ids
        ):
            continue
        rooms.append(room.id)
    return tuple(rooms)


def _group_seat_demand(
    snapshot: BaseSnapshot, member_ids: Iterable[str]
) -> int:
    by_id = {s.id: s for s in snapshot.sessions}
    return sum(by_id[m].seat_demand for m in member_ids if m in by_id)


def compute_domains(
    snapshot: BaseSnapshot,
    constraints: Sequence[EffectiveConstraint] = (),
) -> tuple[Mapping[str, SessionDomain], tuple[Diagnostic, ...]]:
    """Domains for every schedulable session, plus what went wrong.

    Returns blocking diagnostics naming the individual sessions that cannot be
    placed, rather than one anonymous infeasibility for the institution.
    """
    resolved: dict[str, list[EffectiveConstraint]] = {}
    for constraint in constraints:
        resolved.setdefault(constraint.code, []).append(constraint)
    diagnostics: list[Diagnostic] = []
    domains: dict[str, SessionDomain] = {}

    longest_day = max(
        (len(snapshot.slots_by_day(day)) for day in snapshot.period.days),
        default=0,
    )
    if longest_day == 0 or not any(s.is_schedulable for s in snapshot.slots):
        diagnostics.append(
            Diagnostic(
                code=DiagnosticCode.GRID_HAS_NO_SCHEDULABLE_SLOTS,
                message=(
                    f"period {snapshot.period.id} has no schedulable slot; "
                    "nothing can be placed until a grid exists"
                ),
                subject_kind="academic_period",
                subject_id=snapshot.period.id,
            )
        )

    # Seat demand a co-taught session must actually satisfy.
    group_demand: dict[str, int] = {}
    for group in snapshot.co_teaching_groups:
        demand = _group_seat_demand(snapshot, group.member_session_ids)
        for member_id in group.member_session_ids:
            group_demand[member_id] = demand
        if not group.is_approved:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.CO_TEACHING_GROUP_NOT_APPROVED,
                    message=(
                        f"co-teaching group {group.id} is not backed by an "
                        "APPROVED AcademicCompatibilityGroup; merging offerings "
                        "is a human academic act, not one the solver performs"
                    ),
                    subject_kind="session_co_teaching_group",
                    subject_id=group.id,
                )
            )

    for session in snapshot.sessions:
        if not session.requires_timeslot:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.SESSION_NOT_SCHEDULED_BY_DESIGN,
                    message=(
                        f"session {session.id} is asynchronous; it generates no "
                        "decision variable and does not block publication"
                    ),
                    subject_kind="class_session",
                    subject_id=session.id,
                )
            )
            continue

        if session.duration_slots > longest_day:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.SESSION_LONGER_THAN_ANY_DAY,
                    message=(
                        f"session {session.id} needs {session.duration_slots} "
                        f"consecutive slots but the longest day in period "
                        f"{snapshot.period.id} has {longest_day}"
                    ),
                    subject_kind="class_session",
                    subject_id=session.id,
                    detail={
                        "duration_slots": session.duration_slots,
                        "longest_day_slots": longest_day,
                    },
                )
            )

        starts = feasible_starts_for(session, snapshot, resolved)
        rooms = feasible_rooms_for(
            session,
            snapshot,
            resolved,
            seat_demand=group_demand.get(session.id),
        )

        if not starts:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.SESSION_HAS_NO_FEASIBLE_START,
                    message=(
                        f"session {session.id} has no start slot: no day offers "
                        f"{session.duration_slots} consecutive schedulable slots "
                        "on which all of its teachers are available"
                    ),
                    subject_kind="class_session",
                    subject_id=session.id,
                    detail={"duration_slots": session.duration_slots},
                )
            )
        if session.requires_room and not rooms:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.SESSION_HAS_NO_FEASIBLE_ROOM,
                    message=(
                        f"session {session.id} has no room: none is active, on "
                        f"campus {session.campus_id}, large enough for "
                        f"{group_demand.get(session.id, session.seat_demand)} "
                        "seats, carrying every required feature, and on its "
                        "allowed-room list"
                    ),
                    subject_kind="class_session",
                    subject_id=session.id,
                    detail={
                        "campus_id": session.campus_id,
                        "seat_demand": group_demand.get(
                            session.id, session.seat_demand
                        ),
                        "required_features": dict(session.required_features),
                    },
                )
            )

        if not session.teacher_ids:
            diagnostics.append(
                Diagnostic(
                    code=DiagnosticCode.SESSION_HAS_NO_TEACHER,
                    message=(
                        f"session {session.id} has no time-occupying teacher; "
                        "it will be scheduled but nobody is committed to teach it"
                    ),
                    subject_kind="class_session",
                    subject_id=session.id,
                )
            )

        domains[session.id] = SessionDomain(
            session_id=session.id,
            start_slot_ids=starts,
            room_ids=rooms,
            needs_room=session.requires_room,
        )

    diagnostics.extend(_availability_diagnostics(snapshot))
    diagnostics.extend(_duration_diagnostics(snapshot))
    return domains, tuple(diagnostics)


def _availability_diagnostics(snapshot: BaseSnapshot) -> list[Diagnostic]:
    """"Nobody recorded any availability" is not "available all week".

    Both are an empty deny-list and therefore the same input to the solver.
    Only a diagnostic distinguishes them, and the difference is a timetable
    the teacher rejects (STATE.md, "Default availability semantics").
    """
    out: list[Diagnostic] = []
    for teacher in snapshot.teachers:
        if not teacher.has_declared_availability and not teacher.unavailable_slot_ids:
            out.append(
                Diagnostic(
                    code=DiagnosticCode.TEACHER_AVAILABILITY_UNDECLARED,
                    message=(
                        f"teacher {teacher.id} has no availability on record; "
                        "they will be treated as free at every hour of the week"
                    ),
                    subject_kind="teacher",
                    subject_id=teacher.id,
                )
            )
    for room in snapshot.rooms:
        if not room.has_declared_availability and not room.unavailable_slot_ids:
            out.append(
                Diagnostic(
                    code=DiagnosticCode.ROOM_AVAILABILITY_UNDECLARED,
                    message=(
                        f"room {room.id} has no availability on record; it will "
                        "be treated as usable at every hour of the week"
                    ),
                    subject_kind="room",
                    subject_id=room.id,
                )
            )
    return out


def _duration_diagnostics(snapshot: BaseSnapshot) -> list[Diagnostic]:
    """A duration the grid cannot express must be named, not rounded (ADR-0006)."""
    out: list[Diagnostic] = []
    slot_minutes = snapshot.period.slot_minutes
    for session in snapshot.sessions:
        if not session.requires_timeslot:
            continue
        contact = session.contact_minutes
        if contact is None:
            continue
        if contact % slot_minutes:
            out.append(
                Diagnostic(
                    code=DiagnosticCode.SESSION_DURATION_NOT_EXPRESSIBLE,
                    message=(
                        f"session {session.id} runs {contact} minutes, which is "
                        f"not a multiple of the {slot_minutes}-minute grid of "
                        f"period {snapshot.period.id}"
                    ),
                    subject_kind="class_session",
                    subject_id=session.id,
                    detail={
                        "contact_minutes": contact,
                        "slot_minutes": slot_minutes,
                    },
                )
            )
    return out
