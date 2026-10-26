"""Independent validation of a returned schedule.

Deliberately written against the *domain model*, not against the CP-SAT model
that produced the schedule. It re-derives every check from `BaseSnapshot` and
the catalog, so a bug in an adapter — a disjunction posted over the wrong set,
a domain pruned too generously — shows up as a validation failure rather than
as a confident OPTIMAL.

A validator that shared code with the builder would agree with it by
construction, which is the one thing a validator must not do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from academic_ai.domain.scheduling.problem import (
    ProblemInstance,
    classify_student_conflict,
)

__all__ = [
    "Assignment",
    "UnresolvedIncidence",
    "ValidationReport",
    "Violation",
    "unchecked_hard_codes",
    "validate",
]

#: HARD codes this validator knows how to re-derive. `unchecked_hard_codes()`
#: compares it against the catalog, so adding a HARD constraint without adding
#: a check here fails loudly — the same safety property `missing_adapters()`
#: gives the builder. A validator that silently skips a constraint agrees with
#: the builder by omission, which is the one thing it must not do.
_CHECKED_CODES = frozenset(
    {
        "ASSIGNMENT_LOCKED",
        "CO_TEACHING_SAME_ROOM",
        "CO_TEACHING_SAME_TIMESLOT",
        "ROOM_ACTIVE",
        "ROOM_AVAILABILITY",
        "ROOM_CAMPUS_MATCH",
        "ROOM_CAPACITY",
        "ROOM_FEATURE_REQUIREMENT",
        "ROOM_NO_OVERLAP",
        "ROOM_TYPE_ALLOWED",
        "SECTION_SESSIONS_NO_OVERLAP",
        "SESSION_PINNED",
        "SESSION_SLOTS_SCHEDULABLE",
        "SESSION_WITHIN_DAY_GRID",
        "STUDENT_CAMPUS_TRAVEL",
        "STUDENT_NO_OVERLAP_UNRESOLVABLE",
        "TEACHER_AVAILABILITY",
        "TEACHER_CAMPUS_TRAVEL",
        "TEACHER_NO_OVERLAP",
    }
)


def unchecked_hard_codes() -> frozenset[str]:
    """Solver-enforced HARD codes this validator does not re-derive.

    Must be empty. A HARD constraint checked by the builder but not here has
    no independent confirmation at all, and the builder is exactly what the
    validator exists to disbelieve.
    """
    from academic_ai.domain.scheduling.catalog import solver_hard_codes

    return solver_hard_codes() - _CHECKED_CODES


@dataclass(frozen=True, slots=True)
class Assignment:
    """One session placed in time and space."""

    session_id: str
    start_slot_id: str
    room_id: str | None = None


@dataclass(frozen=True, slots=True)
class Violation:
    code: str
    message: str
    session_ids: tuple[str, ...] = ()

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.code}: {self.message}"


@dataclass(frozen=True, slots=True)
class UnresolvedIncidence:
    """A student whose clash was called resolvable and turned out not to be.

    `has_alternative` is static and optimistic by design (ADR-0009 §5): it
    asks whether an alternative section or offering *exists*, not whether it
    is free in the schedule finally produced. Making the HARD boundary
    schedule-dependent needs a disjunction over sections and is a different
    model.

    The recorded price of that optimism was that the validator would name the
    students it stranded anyway. This is that check. It is **not** a
    `Violation` — the schedule is legal — but it is the single fact this
    platform exists to surface, so it must not be silent.
    """

    student_id: str
    session_a: str
    session_b: str
    alternatives_considered: int
    criticality: int
    message: str


@dataclass(frozen=True, slots=True)
class ValidationReport:
    violations: tuple[Violation, ...] = ()
    checked: tuple[str, ...] = ()
    #: Legal, but somebody is stranded. Reported separately from violations
    #: because a registrar must act on these, not a scheduler.
    unresolved_incidences: tuple[UnresolvedIncidence, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not self.violations

    @property
    def has_stranded_students(self) -> bool:
        return bool(self.unresolved_incidences)

    def stranded_students(self) -> tuple[str, ...]:
        return tuple(sorted({i.student_id for i in self.unresolved_incidences}))

    def by_code(self) -> Mapping[str, tuple[Violation, ...]]:
        out: dict[str, list[Violation]] = {}
        for violation in self.violations:
            out.setdefault(violation.code, []).append(violation)
        return {code: tuple(items) for code, items in out.items()}


def validate(
    instance: ProblemInstance, assignments: tuple[Assignment, ...]
) -> ValidationReport:
    """Check a schedule against the HARD constraints of its instance."""
    snapshot = instance.snapshot
    slots = {s.id: s for s in snapshot.slots}
    rooms = {r.id: r for r in snapshot.rooms}
    sessions = {s.id: s for s in snapshot.sessions}
    placed = {a.session_id: a for a in assignments}
    hard = instance.hard_codes()

    violations: list[Violation] = []
    checked: list[str] = []

    def occupied(assignment: Assignment) -> set[tuple[str, int]]:
        """(day, in-day index) pairs the assignment covers."""
        slot = slots[assignment.start_slot_id]
        duration = sessions[assignment.session_id].duration_slots
        return {
            (slot.day_of_week.value, slot.in_day_index + offset)
            for offset in range(duration)
        }

    # --- completeness --------------------------------------------------------
    checked.append("COVERAGE")
    for session in snapshot.sessions:
        if not session.requires_timeslot:
            if session.id in placed:
                violations.append(
                    Violation(
                        "COVERAGE",
                        f"asynchronous session {session.id} was scheduled",
                        (session.id,),
                    )
                )
            continue
        if session.id not in placed:
            violations.append(
                Violation(
                    "COVERAGE",
                    f"session {session.id} has no assignment",
                    (session.id,),
                )
            )

    # --- structural ----------------------------------------------------------
    for assignment in assignments:
        session = sessions[assignment.session_id]
        slot = slots.get(assignment.start_slot_id)
        if slot is None:
            violations.append(
                Violation(
                    "SESSION_WITHIN_DAY_GRID",
                    f"{session.id} starts in unknown slot {assignment.start_slot_id}",
                    (session.id,),
                )
            )
            continue

        day_slots = {s.in_day_index: s for s in snapshot.slots_by_day(slot.day_of_week)}
        span = range(slot.in_day_index, slot.in_day_index + session.duration_slots)
        if any(index not in day_slots for index in span):
            violations.append(
                Violation(
                    "SESSION_WITHIN_DAY_GRID",
                    f"{session.id} runs past the end of {slot.day_of_week.value}",
                    (session.id,),
                )
            )
        elif any(not day_slots[index].is_schedulable for index in span):
            violations.append(
                Violation(
                    "SESSION_SLOTS_SCHEDULABLE",
                    f"{session.id} spans a blocked slot",
                    (session.id,),
                )
            )

        if session.requires_room and assignment.room_id is None:
            violations.append(
                Violation(
                    "SESSION_ROOM_PRESENCE",
                    f"{session.id} needs a room but has none",
                    (session.id,),
                )
            )
        if not session.requires_room and assignment.room_id is not None:
            violations.append(
                Violation(
                    "SESSION_ROOM_PRESENCE",
                    f"{session.id} needs no room but was given {assignment.room_id}",
                    (session.id,),
                )
            )

        # --- room suitability ------------------------------------------------
        if assignment.room_id is None:
            continue
        room = rooms.get(assignment.room_id)
        if room is None:
            violations.append(
                Violation(
                    "ROOM_ACTIVE",
                    f"{session.id} placed in unknown room {assignment.room_id}",
                    (session.id,),
                )
            )
            continue
        if "ROOM_ACTIVE" in hard and not room.is_active:
            violations.append(
                Violation(
                    "ROOM_ACTIVE",
                    f"{session.id} placed in inactive room {room.id}",
                    (session.id,),
                )
            )
        if "ROOM_CAMPUS_MATCH" in hard and room.campus_id != session.campus_id:
            violations.append(
                Violation(
                    "ROOM_CAMPUS_MATCH",
                    f"{session.id} (campus {session.campus_id}) placed in "
                    f"{room.id} on campus {room.campus_id}",
                    (session.id,),
                )
            )
        if "ROOM_TYPE_ALLOWED" in hard and session.allowed_room_ids:
            if room.id not in session.allowed_room_ids:
                violations.append(
                    Violation(
                        "ROOM_TYPE_ALLOWED",
                        f"{session.id} is restricted to "
                        f"{sorted(session.allowed_room_ids)} but was placed in "
                        f"{room.id}",
                        (session.id,),
                    )
                )
        if "ROOM_FEATURE_REQUIREMENT" in hard:
            for feature, needed in session.required_features.items():
                if room.features.get(feature, 0) < needed:
                    violations.append(
                        Violation(
                            "ROOM_FEATURE_REQUIREMENT",
                            f"{session.id} needs {needed}x {feature}; "
                            f"{room.id} has {room.features.get(feature, 0)}",
                            (session.id,),
                        )
                    )
        if "ROOM_AVAILABILITY" in hard and room.unavailable_slot_ids:
            span_ids = {
                s.id
                for s in snapshot.slots_by_day(slot.day_of_week)
                if slot.in_day_index
                <= s.in_day_index
                < slot.in_day_index + session.duration_slots
            }
            if span_ids & room.unavailable_slot_ids:
                violations.append(
                    Violation(
                        "ROOM_AVAILABILITY",
                        f"{room.id} is closed during {session.id}",
                        (session.id,),
                    )
                )

    # --- capacity ------------------------------------------------------------
    if "ROOM_CAPACITY" in hard:
        checked.append("ROOM_CAPACITY")
        group_of = {
            member: group.id
            for group in snapshot.co_teaching_groups
            for member in group.member_session_ids
        }
        demand: dict[str, int] = {}
        for assignment in assignments:
            session = sessions[assignment.session_id]
            key = group_of.get(session.id, session.id)
            demand[key] = demand.get(key, 0) + session.seat_demand
        for assignment in assignments:
            if assignment.room_id is None:
                continue
            session = sessions[assignment.session_id]
            key = group_of.get(session.id, session.id)
            room = rooms[assignment.room_id]
            if room.capacity < demand[key]:
                violations.append(
                    Violation(
                        "ROOM_CAPACITY",
                        f"{room.id} seats {room.capacity}; {key} needs {demand[key]}",
                        (session.id,),
                    )
                )

    # --- teacher availability and overlap -----------------------------------
    teachers = {t.id: t for t in snapshot.teachers}
    if "TEACHER_AVAILABILITY" in hard:
        checked.append("TEACHER_AVAILABILITY")
        for assignment in assignments:
            session = sessions[assignment.session_id]
            slot = slots[assignment.start_slot_id]
            span_ids = {
                s.id
                for s in snapshot.slots_by_day(slot.day_of_week)
                if slot.in_day_index
                <= s.in_day_index
                < slot.in_day_index + session.duration_slots
            }
            for teacher_id in session.teacher_ids:
                teacher = teachers.get(teacher_id)
                if teacher and span_ids & teacher.unavailable_slot_ids:
                    violations.append(
                        Violation(
                            "TEACHER_AVAILABILITY",
                            f"{teacher_id} is unavailable during {session.id}",
                            (session.id,),
                        )
                    )

    co_taught_group = {
        member: group.id
        for group in snapshot.co_teaching_groups
        for member in group.member_session_ids
    }

    def _pairwise_overlap(
        code: str,
        groups: Mapping[str, list[str]],
        *,
        exempt_co_taught: bool = False,
    ) -> None:
        """Report two sessions of one owner occupying the same cell.

        ``exempt_co_taught`` covers the merged-offering case: a co-teaching
        group is one class taught once, so its members legitimately share a
        teacher and a room at the same time. Without the exemption the
        validator would reject every approved merge — which is exactly what it
        did the first time this ran, correctly disagreeing with a builder that
        had the exemption and a validator that did not.
        """
        checked.append(code)
        for owner, session_ids in sorted(groups.items()):
            occupancy: dict[tuple[str, int], str] = {}
            for session_id in session_ids:
                assignment = placed.get(session_id)
                if assignment is None:
                    continue
                for cell in occupied(assignment):
                    clash = occupancy.get(cell)
                    if clash is None:
                        occupancy[cell] = session_id
                        continue
                    if (
                        exempt_co_taught
                        and co_taught_group.get(clash) is not None
                        and co_taught_group.get(clash)
                        == co_taught_group.get(session_id)
                    ):
                        continue
                    violations.append(
                        Violation(
                            code,
                            f"{owner}: {clash} and {session_id} overlap",
                            (clash, session_id),
                        )
                    )

    if "TEACHER_NO_OVERLAP" in hard:
        by_teacher: dict[str, list[str]] = {}
        for session in snapshot.sessions:
            for teacher_id in session.teacher_ids:
                by_teacher.setdefault(teacher_id, []).append(session.id)
        _pairwise_overlap("TEACHER_NO_OVERLAP", by_teacher, exempt_co_taught=True)

    if "SECTION_SESSIONS_NO_OVERLAP" in hard:
        by_section: dict[str, list[str]] = {}
        for session in snapshot.sessions:
            by_section.setdefault(session.section_id, []).append(session.id)
        _pairwise_overlap("SECTION_SESSIONS_NO_OVERLAP", by_section)

    if "ROOM_NO_OVERLAP" in hard:
        checked.append("ROOM_NO_OVERLAP")
        co_taught = co_taught_group
        occupancy: dict[tuple[str, str, int], str] = {}
        for assignment in assignments:
            if assignment.room_id is None:
                continue
            for day, index in occupied(assignment):
                key = (assignment.room_id, day, index)
                clash = occupancy.get(key)
                if clash is None:
                    occupancy[key] = assignment.session_id
                    continue
                # Co-taught sessions are *supposed* to share a room and a slot.
                if co_taught.get(clash) and co_taught.get(clash) == co_taught.get(
                    assignment.session_id
                ):
                    continue
                violations.append(
                    Violation(
                        "ROOM_NO_OVERLAP",
                        f"{assignment.room_id}: {clash} and "
                        f"{assignment.session_id} overlap",
                        (clash, assignment.session_id),
                    )
                )

    # --- student conflicts ---------------------------------------------------
    if "STUDENT_NO_OVERLAP_UNRESOLVABLE" in hard:
        checked.append("STUDENT_NO_OVERLAP_UNRESOLVABLE")
        by_student: dict[str, list] = {}
        for enrollment in snapshot.enrollments:
            by_student.setdefault(enrollment.student_id, []).append(enrollment)
        for student_id, enrollments in sorted(by_student.items()):
            for i, a in enumerate(enrollments):
                for b in enrollments[i + 1 :]:
                    if (
                        classify_student_conflict(a, b)
                        != "STUDENT_NO_OVERLAP_UNRESOLVABLE"
                    ):
                        continue
                    for sa in a.session_ids:
                        for sb in b.session_ids:
                            if sa == sb or sa not in placed or sb not in placed:
                                continue
                            if occupied(placed[sa]) & occupied(placed[sb]):
                                violations.append(
                                    Violation(
                                        "STUDENT_NO_OVERLAP_UNRESOLVABLE",
                                        f"student {student_id}: {sa} and {sb} "
                                        "clash with no way to re-section",
                                        (sa, sb),
                                    )
                                )

    # --- co-teaching ---------------------------------------------------------
    if "CO_TEACHING_SAME_TIMESLOT" in hard:
        checked.append("CO_TEACHING_SAME_TIMESLOT")
        for group in snapshot.co_teaching_groups:
            members = [m for m in group.member_session_ids if m in placed]
            starts = {placed[m].start_slot_id for m in members}
            if len(starts) > 1:
                violations.append(
                    Violation(
                        "CO_TEACHING_SAME_TIMESLOT",
                        f"group {group.id} members start in different slots",
                        tuple(members),
                    )
                )
            room_ids = {placed[m].room_id for m in members}
            if "CO_TEACHING_SAME_ROOM" in hard and len(room_ids) > 1:
                violations.append(
                    Violation(
                        "CO_TEACHING_SAME_ROOM",
                        f"group {group.id} members are in different rooms",
                        tuple(members),
                    )
                )

    # --- locks ---------------------------------------------------------------
    if "ASSIGNMENT_LOCKED" in hard:
        checked.append("ASSIGNMENT_LOCKED")
        for base in snapshot.base_assignments:
            if not base.is_locked:
                continue
            actual = placed.get(base.session_id)
            if actual is None:
                continue
            if actual.start_slot_id != base.start_slot_id or (
                base.room_id and actual.room_id != base.room_id
            ):
                violations.append(
                    Violation(
                        "ASSIGNMENT_LOCKED",
                        f"{base.session_id} is locked to "
                        f"{base.start_slot_id}/{base.room_id} but was placed at "
                        f"{actual.start_slot_id}/{actual.room_id}",
                        (base.session_id,),
                    )
                )

    # --- durable pins --------------------------------------------------------
    if "SESSION_PINNED" in hard:
        checked.append("SESSION_PINNED")
        for constraint in instance.constraints:
            if constraint.code != "SESSION_PINNED" or not constraint.is_active:
                continue
            params = constraint.parameters
            session_id = constraint.scope.section_id or params.get("class_session_id")
            if not isinstance(session_id, str):
                continue
            actual = placed.get(session_id)
            if actual is None:
                continue
            wanted_slot = params.get("start_slot_id")
            wanted_room = params.get("room_id")
            if isinstance(wanted_slot, str) and actual.start_slot_id != wanted_slot:
                violations.append(
                    Violation(
                        "SESSION_PINNED",
                        f"{session_id} is pinned to {wanted_slot} but was "
                        f"placed at {actual.start_slot_id}",
                        (session_id,),
                    )
                )
            if isinstance(wanted_room, str) and actual.room_id != wanted_room:
                violations.append(
                    Violation(
                        "SESSION_PINNED",
                        f"{session_id} is pinned to room {wanted_room} but was "
                        f"placed in {actual.room_id}",
                        (session_id,),
                    )
                )

    # --- campus travel -------------------------------------------------------
    def _travel_slots(origin: str, destination: str) -> int:
        if origin == destination:
            return 0
        minutes = snapshot.travel_minutes.get((origin, destination))
        if minutes is None:
            return 0
        # ceil: rounding 45 minutes down to 0 on a 60-minute grid would bless
        # a journey nobody can make.
        return -(-minutes // snapshot.period.slot_minutes)

    def _check_travel(code: str, groups: Mapping[str, list[str]]) -> None:
        checked.append(code)
        for owner, session_ids in sorted(groups.items()):
            timed = []
            for session_id in session_ids:
                assignment = placed.get(session_id)
                if assignment is None or assignment.room_id is None:
                    continue
                slot = slots.get(assignment.start_slot_id)
                room = rooms.get(assignment.room_id)
                if slot is None or room is None:
                    continue
                timed.append(
                    (
                        slot.day_of_week.value,
                        slot.in_day_index,
                        sessions[session_id].duration_slots,
                        room.campus_id,
                        session_id,
                    )
                )
            for i, first in enumerate(timed):
                for second in timed[i + 1 :]:
                    # Same day only. The occupancy index is a disjunction axis,
                    # not a time axis: charging travel across an overnight gap
                    # would forbid a teacher simply working two campuses on two
                    # days.
                    if first[0] != second[0] or first[3] == second[3]:
                        continue
                    earlier, later = sorted((first, second), key=lambda t: t[1])
                    gap = later[1] - (earlier[1] + earlier[2])
                    needed = _travel_slots(earlier[3], later[3])
                    if gap < needed:
                        violations.append(
                            Violation(
                                code,
                                f"{owner}: {earlier[4]} on {earlier[3]} and "
                                f"{later[4]} on {later[3]} are {gap} slot(s) "
                                f"apart but need {needed}",
                                (earlier[4], later[4]),
                            )
                        )

    if "TEACHER_CAMPUS_TRAVEL" in hard:
        by_teacher_travel: dict[str, list[str]] = {}
        for session in snapshot.sessions:
            for teacher_id in session.teacher_ids:
                by_teacher_travel.setdefault(teacher_id, []).append(session.id)
        _check_travel("TEACHER_CAMPUS_TRAVEL", by_teacher_travel)

    if "STUDENT_CAMPUS_TRAVEL" in hard:
        by_student_travel: dict[str, list[str]] = {}
        for enrollment in snapshot.enrollments:
            by_student_travel.setdefault(enrollment.student_id, []).extend(
                enrollment.session_ids
            )
        _check_travel("STUDENT_CAMPUS_TRAVEL", by_student_travel)

    # --- students the optimistic predicate stranded anyway -------------------
    unresolved = _unresolved_incidences(instance, placed, occupied)

    return ValidationReport(
        tuple(violations), tuple(dict.fromkeys(checked)), unresolved
    )


def _unresolved_incidences(
    instance: ProblemInstance,
    placed: Mapping[str, Assignment],
    occupied,
) -> tuple[UnresolvedIncidence, ...]:
    """MANDATORY clashes whose alternatives are all busy in *this* schedule.

    Required by OPTIMIZER.md §6.6 and by ADR-0009 §5, which recorded this as
    the price of a static `has_alternative`. A clash classified MANDATORY says
    "a registrar could move this student" — but if every alternative section is
    itself occupied at that moment, nobody can, and the student is stranded by
    a schedule the model calls legal.

    Reported separately from violations: the schedule is not invalid, and
    telling a scheduler to fix it would be telling the wrong person.
    """
    from academic_ai.domain.scheduling.students import criticality

    snapshot = instance.snapshot
    sessions = {s.id: s for s in snapshot.sessions}
    by_student: dict[str, list] = {}
    for enrollment in snapshot.enrollments:
        by_student.setdefault(enrollment.student_id, []).append(enrollment)

    # Which (day, index) cells each offering's sessions occupy, so "is the
    # alternative free?" is answerable from the produced schedule.
    busy_by_offering: dict[str, set] = {}
    for session_id, assignment in placed.items():
        session = sessions.get(session_id)
        if session is None:
            continue
        busy_by_offering.setdefault(session.offering_id, set()).update(
            occupied(assignment)
        )

    out: list[UnresolvedIncidence] = []
    for student_id, enrollments in sorted(by_student.items()):
        for i, a in enumerate(enrollments):
            for b in enrollments[i + 1 :]:
                if (
                    classify_student_conflict(a, b)
                    != "STUDENT_NO_OVERLAP_MANDATORY"
                ):
                    continue
                for sa in a.session_ids:
                    for sb in b.session_ids:
                        if sa == sb or sa not in placed or sb not in placed:
                            continue
                        clash = occupied(placed[sa]) & occupied(placed[sb])
                        if not clash:
                            continue
                        # The clash is real. Is any alternative actually free?
                        alternatives = (
                            a.alternative_section_count
                            + a.alternative_offering_count
                            + b.alternative_section_count
                            + b.alternative_offering_count
                        )
                        others = {
                            offering: cells
                            for offering, cells in busy_by_offering.items()
                            if offering not in (a.offering_id, b.offering_id)
                        }
                        escape = any(not (cells & clash) for cells in others.values())
                        if escape:
                            continue
                        out.append(
                            UnresolvedIncidence(
                                student_id=student_id,
                                session_a=min(sa, sb),
                                session_b=max(sa, sb),
                                alternatives_considered=alternatives,
                                criticality=criticality(a) + criticality(b),
                                message=(
                                    f"student {student_id}: {min(sa, sb)} and "
                                    f"{max(sa, sb)} clash, and every alternative "
                                    "counted as a way out is itself occupied at "
                                    "that time"
                                ),
                            )
                        )
    return tuple(out)
