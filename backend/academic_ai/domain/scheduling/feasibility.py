"""Feasibility analysis: why an instance cannot work, before spending a search.

The failure this module exists to prevent is a bare `INFEASIBLE`. It is a true
statement and the least useful one a planner can be handed: it names nothing,
suggests nothing, and is indistinguishable from a timeout. Every check here
answers the question a coordinator actually has — *what do I change?*

Three severities, and the middle one is the dangerous category:

``ERROR``
    Structurally impossible. No schedule exists and no amount of search will
    find one. Reported instead of a solver status, never alongside it.

``WARNING``
    Solvable, but something the reader believes is enforced is vacuous or
    partial — a teacher with no availability on record, a room whose capacity
    is unknown. The schedule will look fine and be wrong.

``SUGGESTION``
    Nothing is wrong. Something could be better, or is tight enough to be
    worth knowing before it becomes an ERROR next period.

Checks are ordered cheapest-first and are all **pre-solve**: they read the
instance, never a schedule. That is the point — an ERROR found here costs
milliseconds, where the same fact found inside CP-SAT costs the whole budget
and arrives without a subject.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping, Sequence

from academic_ai.domain.enums import RecommendationKind
from academic_ai.domain.scheduling.diagnostics import Diagnostic
from academic_ai.domain.scheduling.domains import compute_domains
from academic_ai.domain.scheduling.problem import (
    ProblemInstance,
    classify_student_conflict,
)
from academic_ai.domain.scheduling.students import build_conflict_graph, criticality

__all__ = [
    "Finding",
    "InfeasibilityReport",
    "Severity",
    "analyse",
]


class Severity(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    ERROR = "ERROR"
    WARNING = "WARNING"
    SUGGESTION = "SUGGESTION"


@dataclass(frozen=True, slots=True)
class Finding:
    """One thing wrong, or worth knowing, about an instance.

    ``subject_id`` is mandatory for an ERROR: "something is impossible" is not
    an improvement on `INFEASIBLE`. ``remedies`` name the kinds of action that
    would address it, so SESSION 6's recommendation provider and SESSION 10's
    UI both have somewhere to start.
    """

    severity: Severity
    code: str
    message: str
    subject_kind: str | None = None
    subject_id: str | None = None
    evidence: Mapping[str, object] = field(default_factory=dict)
    remedies: tuple[RecommendationKind, ...] = ()

    def __post_init__(self) -> None:
        if self.severity is Severity.ERROR and not self.subject_id:
            raise ValueError(
                f"{self.code}: an ERROR must name its subject. An unnamed "
                "impossibility is no more actionable than a bare INFEASIBLE, "
                "which is the answer this module exists to replace."
            )


@dataclass(frozen=True, slots=True)
class InfeasibilityReport:
    """What the analyser found, and whether it is worth solving at all."""

    findings: tuple[Finding, ...] = ()
    diagnostics: tuple[Diagnostic, ...] = ()

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.WARNING)

    @property
    def suggestions(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity is Severity.SUGGESTION)

    @property
    def is_solvable(self) -> bool:
        """False when a structural impossibility was proved without searching."""
        return not self.errors

    def by_code(self) -> Mapping[str, int]:
        return dict(Counter(f.code for f in self.findings))

    def summary(self) -> str:
        """One line a human can act on — never "No solution."."""
        if self.errors:
            first = self.errors[0]
            more = len(self.errors) - 1
            tail = f" (and {more} more)" if more else ""
            return f"{first.message}{tail}"
        if self.warnings:
            return (
                f"solvable, but {len(self.warnings)} thing(s) the schedule "
                "will not enforce as you may expect"
            )
        return "no structural problems found"


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def _unplaceable_sessions(instance: ProblemInstance) -> list[Finding]:
    """Sessions whose variable domain is empty.

    `compute_domains` already names these individually; this turns each into a
    finding with the remedies that would actually fix it. Which remedy applies
    depends on *which* domain is empty: no start is a time problem, no room is
    a space problem, and conflating them sends the coordinator to the wrong
    place.
    """
    domains, _ = compute_domains(instance.snapshot, instance.constraints)
    sessions = {s.id: s for s in instance.snapshot.sessions}
    out: list[Finding] = []

    for session_id, domain in domains.items():
        if not domain.is_empty:
            continue
        session = sessions[session_id]
        if not domain.start_slot_ids:
            out.append(
                Finding(
                    Severity.ERROR,
                    "SESSION_HAS_NO_TIME",
                    f"session {session_id} cannot be placed at any time: it "
                    f"needs {session.duration_slots} consecutive schedulable "
                    "slots on a day when all of its teachers are free",
                    "class_session",
                    session_id,
                    {
                        "duration_slots": session.duration_slots,
                        "teachers": list(session.teacher_ids),
                    },
                    (
                        RecommendationKind.MOVE_SESSION,
                        RecommendationKind.CHANGE_INSTRUCTOR,
                    ),
                )
            )
        elif domain.needs_room and not domain.room_ids:
            out.append(
                Finding(
                    Severity.ERROR,
                    "SESSION_HAS_NO_ROOM",
                    f"session {session_id} has no usable room: none is active, "
                    f"on campus {session.campus_id}, large enough for "
                    f"{session.seat_demand} seats, carrying every required "
                    "feature, and on its allowed-room list",
                    "class_session",
                    session_id,
                    {
                        "seat_demand": session.seat_demand,
                        "campus_id": session.campus_id,
                        "required_features": dict(session.required_features),
                        "allowed_rooms": sorted(session.allowed_room_ids),
                    },
                    (
                        RecommendationKind.CHANGE_ROOM,
                        RecommendationKind.SPLIT_SECTION,
                    ),
                )
            )
    return out


def _teacher_load(instance: ProblemInstance) -> list[Finding]:
    """Teachers asked for more hours than the week, or than their contract.

    Invariant under the decision variables — no choice of slot changes a
    weekly total — which is why the catalog makes `TEACHER_MAX_WEEKLY_HOURS` a
    precondition. Handing it to CP-SAT would report "your timetable is
    infeasible" for a staffing decision.
    """
    snapshot = instance.snapshot
    slot_minutes = snapshot.period.slot_minutes
    schedulable_per_week = sum(1 for s in snapshot.slots if s.is_schedulable)

    demand: dict[str, int] = {}
    for session in snapshot.scheduled_sessions():
        for teacher_id in session.teacher_ids:
            demand[teacher_id] = demand.get(teacher_id, 0) + session.duration_slots

    out: list[Finding] = []
    for teacher in snapshot.teachers:
        required = demand.get(teacher.id, 0)
        if not required:
            continue

        available = schedulable_per_week - len(teacher.unavailable_slot_ids)
        if required > available:
            out.append(
                Finding(
                    Severity.ERROR,
                    "TEACHER_HOURS_EXCEED_AVAILABILITY",
                    f"teacher {teacher.id} is assigned {required} slots but is "
                    f"available for only {available}",
                    "teacher",
                    teacher.id,
                    {"required_slots": required, "available_slots": available},
                    (
                        RecommendationKind.CHANGE_INSTRUCTOR,
                        RecommendationKind.PROPOSE_NEGOTIATION,
                    ),
                )
            )
            continue

        if teacher.max_weekly_minutes is not None:
            required_minutes = required * slot_minutes
            if required_minutes > teacher.max_weekly_minutes:
                out.append(
                    Finding(
                        Severity.ERROR,
                        "TEACHER_WEEKLY_LOAD_EXCEEDED",
                        f"teacher {teacher.id} is assigned {required_minutes} "
                        f"minutes against a limit of {teacher.max_weekly_minutes}",
                        "teacher",
                        teacher.id,
                        {
                            "required_minutes": required_minutes,
                            "limit_minutes": teacher.max_weekly_minutes,
                        },
                        (RecommendationKind.CHANGE_INSTRUCTOR,),
                    )
                )
        elif required > available * 0.85:
            out.append(
                Finding(
                    Severity.SUGGESTION,
                    "TEACHER_NEAR_CAPACITY",
                    f"teacher {teacher.id} fills {required} of {available} "
                    "available slots; there is almost no room to move them",
                    "teacher",
                    teacher.id,
                    {"required_slots": required, "available_slots": available},
                )
            )
    return out


def _room_capacity_pressure(instance: ProblemInstance) -> list[Finding]:
    """Total room-slots against total demand.

    A counting argument, so it holds regardless of how clever the search is:
    if the sessions need more room-hours than exist, no schedule exists.
    """
    snapshot = instance.snapshot
    needing_rooms = [s for s in snapshot.scheduled_sessions() if s.requires_room]
    if not needing_rooms or not snapshot.rooms:
        return []

    schedulable = sum(1 for s in snapshot.slots if s.is_schedulable)
    supply = len([r for r in snapshot.rooms if r.is_active]) * schedulable
    demand = sum(s.duration_slots for s in needing_rooms)

    if demand > supply:
        return [
            Finding(
                Severity.ERROR,
                "INSUFFICIENT_ROOM_SLOTS",
                f"the timetable needs {demand} room-slots but only {supply} "
                "exist; no arrangement can fit",
                "academic_period",
                snapshot.period.id,
                {"demand": demand, "supply": supply},
                (RecommendationKind.CREATE_SECTION, RecommendationKind.MERGE_OFFERINGS),
            )
        ]
    if demand > supply * 0.9:
        return [
            Finding(
                Severity.SUGGESTION,
                "ROOM_SUPPLY_TIGHT",
                f"{demand} of {supply} room-slots are needed ({demand / supply:.0%}); "
                "the search will have very little freedom",
                "academic_period",
                snapshot.period.id,
                {"demand": demand, "supply": supply},
            )
        ]
    return []


def _structural_student_conflicts(instance: ProblemInstance) -> list[Finding]:
    """Students whose enrollments cannot all be attended, whatever the schedule.

    A student enrolled in more slot-hours than the week holds is stranded by
    arithmetic, not by an unlucky arrangement — and that is worth saying before
    a search spends a minute failing to fix it.
    """
    snapshot = instance.snapshot
    schedulable = sum(1 for s in snapshot.slots if s.is_schedulable)
    sessions = {s.id: s for s in snapshot.sessions}

    by_student: dict[str, set[str]] = {}
    for enrollment in snapshot.enrollments:
        by_student.setdefault(enrollment.student_id, set()).update(
            s for s in enrollment.session_ids if s in sessions
        )

    out: list[Finding] = []
    for student_id, session_ids in sorted(by_student.items()):
        required = sum(sessions[s].duration_slots for s in session_ids)
        if required > schedulable:
            out.append(
                Finding(
                    Severity.ERROR,
                    "STUDENT_LOAD_EXCEEDS_WEEK",
                    f"student {student_id} is enrolled in {required} slots but "
                    f"the week has only {schedulable}",
                    "student",
                    student_id,
                    {"required_slots": required, "week_slots": schedulable},
                    (RecommendationKind.PROPOSE_NEGOTIATION,),
                )
            )
    return out


def _unresolvable_conflict_pressure(instance: ProblemInstance) -> list[Finding]:
    """Students whose clashes no registrar action can resolve.

    Not an ERROR: whether these become real depends on the schedule. But they
    are the population the platform exists to protect, so they are surfaced
    before the search rather than discovered after it.
    """
    graph = build_conflict_graph(instance)
    unresolvable = [e for e in graph.edges if e.is_unresolvable]
    if not unresolvable:
        return []

    stranded = sorted({s for e in unresolvable for s in e.student_ids})
    return [
        Finding(
            Severity.WARNING,
            "UNRESOLVABLE_CONFLICT_RISK",
            f"{len(stranded)} student(s) have enrollments that cannot be "
            f"re-sectioned across {len(unresolvable)} session pair(s); if any "
            "of those pairs ends up simultaneous, those students are stranded",
            "student",
            None,
            {
                "students": stranded[:50],
                "student_count": len(stranded),
                "pairs": len(unresolvable),
                "total_criticality": sum(e.weight for e in unresolvable),
            },
            (
                RecommendationKind.CREATE_SECTION,
                RecommendationKind.USE_SHARED_OFFERING,
            ),
        )
    ]


def _undeclared_data(instance: ProblemInstance) -> list[Finding]:
    """Things the model will treat as permissive because nobody said otherwise.

    "No availability on record" and "available every hour" are the same empty
    deny-list and completely different facts. The schedule will look fine and
    the teacher will reject it.
    """
    snapshot = instance.snapshot
    out: list[Finding] = []

    silent_teachers = [
        t.id
        for t in snapshot.teachers
        if not t.has_declared_availability and not t.unavailable_slot_ids
    ]
    if silent_teachers:
        out.append(
            Finding(
                Severity.WARNING,
                "TEACHER_AVAILABILITY_UNDECLARED",
                f"{len(silent_teachers)} teacher(s) have no availability on "
                "record and will be treated as free at every hour of the week",
                "teacher",
                None,
                {"teachers": sorted(silent_teachers)[:50]},
                (RecommendationKind.PROPOSE_NEGOTIATION,),
            )
        )

    unstaffed = [
        s.id
        for s in snapshot.scheduled_sessions()
        if not s.teacher_ids
    ]
    if unstaffed:
        out.append(
            Finding(
                Severity.WARNING,
                "SESSION_HAS_NO_TEACHER",
                f"{len(unstaffed)} session(s) will be scheduled with nobody "
                "committed to teach them",
                "class_session",
                None,
                {"sessions": sorted(unstaffed)[:50]},
                (RecommendationKind.CHANGE_INSTRUCTOR,),
            )
        )
    return out


def _unapproved_merges(instance: ProblemInstance) -> list[Finding]:
    """A merge nobody approved is not a merge (ADR-0004)."""
    return [
        Finding(
            Severity.ERROR,
            "CO_TEACHING_GROUP_NOT_APPROVED",
            f"co-teaching group {group.id} is not backed by an APPROVED "
            "compatibility group; merging offerings is a human academic act",
            "session_co_teaching_group",
            group.id,
            {"members": list(group.member_session_ids)},
            (RecommendationKind.PROPOSE_NEGOTIATION,),
        )
        for group in instance.snapshot.co_teaching_groups
        if not group.is_approved
    ]


_CHECKS = (
    _unapproved_merges,
    _teacher_load,
    _room_capacity_pressure,
    _structural_student_conflicts,
    _unplaceable_sessions,
    _unresolvable_conflict_pressure,
    _undeclared_data,
)


def analyse(instance: ProblemInstance) -> InfeasibilityReport:
    """Every structural problem, before any search.

    Runs all checks rather than stopping at the first ERROR: a coordinator
    handed one impossibility at a time, each costing a round trip, is being
    made to play twenty questions with their own timetable.
    """
    findings: list[Finding] = []
    for check in _CHECKS:
        findings.extend(check(instance))

    _, diagnostics = compute_domains(instance.snapshot, instance.constraints)

    order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.SUGGESTION: 2}
    findings.sort(key=lambda f: (order[f.severity], f.code, f.subject_id or ""))
    return InfeasibilityReport(tuple(findings), tuple(diagnostics))
