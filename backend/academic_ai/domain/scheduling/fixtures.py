"""Deterministic mathematical fixtures.

Each fixture is a hand-verifiable instance isolating **one** phenomenon, with
a stated, provable answer. They exist so that SESSION 3 can assert its CP-SAT
adapters reproduce a known result rather than merely "a result", and so
SESSION 4 can claim an LNS improvement against a fixed baseline.

Three expectations, and the difference between the last two is the point:

``FEASIBLE``
    A schedule exists. Where the fixture is tight enough to have exactly one,
    ``unique_solution`` states it, so an adapter that is subtly wrong is
    caught by a value rather than by a status.

``INFEASIBLE``
    No schedule exists, and the solver must **prove** it. This is an
    interaction between sessions — the kind of thing only a solver can settle.

``BLOCKED``
    The instance must be rejected *before the solver runs*, with a named
    diagnostic. A BLOCKED fixture that reaches CP-SAT and comes back
    INFEASIBLE is a bug even though the status looks right: "no solution
    exists" and "you gave three sections one room that seats twenty and asked
    for sixty" are different answers, and only one of them tells anybody what
    to do (playbook SESSION 6: never return only "No solution.").

Everything is small on purpose — one or two days, a handful of slots. A
fixture nobody can check by hand is not a fixture, it is a regression trap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping

from academic_ai.domain.enums import ConstraintCategory, DayOfWeek, SessionType
from academic_ai.domain.scheduling.diagnostics import DiagnosticCode
from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
from academic_ai.domain.scheduling.problem import (
    BaseAssignment,
    BaseSnapshot,
    CoTeachingGroupSpec,
    EffectiveConstraint,
    PeriodSpec,
    ProblemInstance,
    RoomSpec,
    SessionSpec,
    SlotSpec,
    StudentEnrollmentSpec,
    TeacherSpec,
)

__all__ = [
    "FIXTURES",
    "Expectation",
    "Fixture",
    "get_fixture",
]

_MON = DayOfWeek.MONDAY
_TUE = DayOfWeek.TUESDAY


class Expectation(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    FEASIBLE = "FEASIBLE"
    INFEASIBLE = "INFEASIBLE"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class Fixture:
    """One instance with a known answer."""

    code: str
    #: What this isolates, and why the answer is what it is.
    description: str
    expectation: Expectation
    build: Callable[[], ProblemInstance]
    #: Catalog codes this fixture exercises.
    isolates: tuple[str, ...] = ()
    #: Diagnostics a correct builder must emit. For BLOCKED fixtures at least
    #: one of these is blocking.
    expected_diagnostics: tuple[DiagnosticCode, ...] = ()
    #: session id -> (start slot id, room id or None). Stated only where the
    #: fixture admits exactly one schedule.
    unique_solution: Mapping[str, tuple[str, str | None]] | None = None
    objective_profile: str = "GREENFIELD"

    def instance(self) -> ProblemInstance:
        return self.build()


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def _slots(day: DayOfWeek, count: int, *, blocked: frozenset[int] = frozenset()):
    """``count`` hourly slots from 08:00 on one day."""
    return tuple(
        SlotSpec(
            id=f"{day.value[:3].lower()}{i}",
            day_of_week=day,
            in_day_index=i,
            global_index=(0 if day is _MON else 100) + i,
            start_minute=(8 + i) * 60,
            is_schedulable=i not in blocked,
        )
        for i in range(count)
    )


def _room(
    id_: str = "R1",
    *,
    capacity: int = 40,
    campus: str = "C1",
    features: Mapping[str, int] | None = None,
    active: bool = True,
) -> RoomSpec:
    return RoomSpec(
        id=id_,
        campus_id=campus,
        capacity=capacity,
        room_type="CLASSROOM",
        features=dict(features or {}),
        is_active=active,
        has_declared_availability=True,
    )


def _teacher(id_: str = "T1", *, unavailable: frozenset[str] = frozenset()) -> TeacherSpec:
    return TeacherSpec(
        id=id_,
        unavailable_slot_ids=unavailable,
        has_declared_availability=True,
    )


def _session(
    id_: str,
    *,
    duration: int = 1,
    teachers: tuple[str, ...] = ("T1",),
    seats: int = 20,
    campus: str = "C1",
    features: Mapping[str, int] | None = None,
    requires_room: bool = True,
    requires_timeslot: bool = True,
    group: str | None = None,
    contact_minutes: int | None = None,
) -> SessionSpec:
    return SessionSpec(
        id=id_,
        section_id=f"{id_}-sec",
        offering_id=f"{id_}-off",
        course_id=f"{id_}-crs",
        campus_id=campus,
        duration_slots=duration,
        session_type=SessionType.LECTURE,
        requires_timeslot=requires_timeslot,
        requires_room=requires_room,
        teacher_ids=teachers,
        required_features=dict(features or {}),
        seat_demand=seats,
        co_teaching_group_id=group,
        contact_minutes=contact_minutes,
    )


def _instance(
    snapshot: BaseSnapshot,
    *,
    constraints: tuple[EffectiveConstraint, ...] = (),
    profile: str = "GREENFIELD",
) -> ProblemInstance:
    return ProblemInstance(
        snapshot=snapshot,
        constraints=constraints,
        objective_profile=OBJECTIVE_PROFILES[profile],
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _trivial_feasible() -> ProblemInstance:
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(_room(),),
            teachers=(_teacher(),),
            sessions=(_session("S1"),),
        )
    )


def _teacher_clash_forced() -> ProblemInstance:
    """Two sessions, one teacher, one slot. The clash cannot be avoided."""
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 1),
            rooms=(_room("R1"), _room("R2")),
            teachers=(_teacher(),),
            sessions=(_session("S1"), _session("S2")),
        )
    )


def _teacher_clash_avoidable() -> ProblemInstance:
    """Same teacher, two slots, two sessions — exactly one schedule up to
    the symmetry the unique_solution pins down."""
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(_room("R1"),),
            teachers=(_teacher(),),
            sessions=(_session("S1"), _session("S2")),
        )
    )


def _room_contention_forced() -> ProblemInstance:
    """Two sessions, different teachers, one room, one slot."""
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 1),
            rooms=(_room("R1"),),
            teachers=(_teacher("T1"), _teacher("T2")),
            sessions=(_session("S1"), _session("S2", teachers=("T2",))),
        )
    )


def _lunch_band_blocks_long_session() -> ProblemInstance:
    """The reason the grid is dense (ADR-0006).

    Slots 0..3 exist; slot 2 is a blocked lunch band. A 3-slot session has no
    feasible start: starting at 0 spans the blocked slot 2, and starting at 1
    or later runs past the end of the day.
    """
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 4, blocked=frozenset({2})),
            rooms=(_room(),),
            teachers=(_teacher(),),
            sessions=(_session("S1", duration=3),),
        )
    )


def _session_longer_than_day() -> ProblemInstance:
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(_room(),),
            teachers=(_teacher(),),
            sessions=(_session("S1", duration=3),),
        )
    )


def _no_room_large_enough() -> ProblemInstance:
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(_room(capacity=10),),
            teachers=(_teacher(),),
            sessions=(_session("S1", seats=60),),
        )
    )


def _no_room_with_feature() -> ProblemInstance:
    """A chemistry lab with no fume hood is a hazard, not a timetable."""
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(_room(features={"PROJECTOR": 1}),),
            teachers=(_teacher(),),
            sessions=(_session("S1", features={"FUME_HOOD": 4}),),
        )
    )


def _teacher_unavailable_everywhere() -> ProblemInstance:
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(_room(),),
            teachers=(_teacher(unavailable=frozenset({"mon0", "mon1"})),),
            sessions=(_session("S1"),),
        )
    )


def _student_conflict_unresolvable() -> ProblemInstance:
    """The population the platform exists to protect.

    One student, two mandatory courses, each with a single section, and one
    slot in the week. Neither enrollment has an alternative section, so
    `classify_student_conflict` returns UNRESOLVABLE — HARD by default, and
    the instance is genuinely infeasible.
    """
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 1),
            rooms=(_room("R1"), _room("R2")),
            teachers=(_teacher("T1"), _teacher("T2")),
            sessions=(_session("S1"), _session("S2", teachers=("T2",))),
            enrollments=(
                StudentEnrollmentSpec(
                    student_id="E1",
                    offering_id="S1-off",
                    session_ids=("S1",),
                    is_mandatory=True,
                    alternative_section_count=0,
                    is_repeat=True,
                ),
                StudentEnrollmentSpec(
                    student_id="E1",
                    offering_id="S2-off",
                    session_ids=("S2",),
                    is_mandatory=True,
                    alternative_section_count=0,
                ),
            ),
        )
    )


def _student_conflict_avoidable() -> ProblemInstance:
    """The same clash, but two slots exist, so a schedule avoids it entirely."""
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(_room("R1"), _room("R2")),
            teachers=(_teacher("T1"), _teacher("T2")),
            sessions=(_session("S1"), _session("S2", teachers=("T2",))),
            enrollments=(
                StudentEnrollmentSpec(
                    student_id="E1",
                    offering_id="S1-off",
                    session_ids=("S1",),
                    is_mandatory=True,
                ),
                StudentEnrollmentSpec(
                    student_id="E1",
                    offering_id="S2-off",
                    session_ids=("S2",),
                    is_mandatory=True,
                ),
            ),
        )
    )


def _student_conflict_elective_tolerated() -> ProblemInstance:
    """One slot, one student, two courses — but one is elective.

    Feasible: the clash is an objective term, not a constraint. Reaching
    INFEASIBLE here would mean the model silently promoted every student
    conflict to HARD, which SESSION 5 forbids by name.
    """
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 1),
            rooms=(_room("R1"), _room("R2")),
            teachers=(_teacher("T1"), _teacher("T2")),
            sessions=(_session("S1"), _session("S2", teachers=("T2",))),
            enrollments=(
                StudentEnrollmentSpec(
                    student_id="E1",
                    offering_id="S1-off",
                    session_ids=("S1",),
                    is_mandatory=True,
                ),
                StudentEnrollmentSpec(
                    student_id="E1",
                    offering_id="S2-off",
                    session_ids=("S2",),
                    is_mandatory=False,
                ),
            ),
        )
    )


def _co_teaching_merged() -> ProblemInstance:
    """Two sessions of different offerings taught together.

    Seat demand is the sum (20 + 20), so only the 50-seat room admits the
    group — the 30-seat room would suffice for either alone and must be
    pruned anyway (ADR-0004).
    """
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(_room("SMALL", capacity=30), _room("BIG", capacity=50)),
            teachers=(_teacher(),),
            sessions=(
                _session("S1", group="G1"),
                _session("S2", group="G1"),
            ),
            co_teaching_groups=(
                CoTeachingGroupSpec(
                    id="G1",
                    member_session_ids=("S1", "S2"),
                    is_approved=True,
                ),
            ),
        )
    )


def _co_teaching_unapproved() -> ProblemInstance:
    """An AI may propose a merge; only a human approves one (ADR-0004)."""
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(_room("BIG", capacity=50),),
            teachers=(_teacher(),),
            sessions=(
                _session("S1", group="G1"),
                _session("S2", group="G1"),
            ),
            co_teaching_groups=(
                CoTeachingGroupSpec(
                    id="G1", member_session_ids=("S1", "S2"), is_approved=False
                ),
            ),
        )
    )


def _availability_undeclared() -> ProblemInstance:
    """Solvable, and quietly wrong.

    Nobody recorded this teacher's availability. The solver sees the same
    empty deny-list it would see for a teacher genuinely free all week, and
    will happily schedule them at 08:00 Monday. Only the diagnostic separates
    the two facts.
    """
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(RoomSpec(id="R1", campus_id="C1", capacity=40, room_type="CLASSROOM"),),
            teachers=(TeacherSpec(id="T1"),),
            sessions=(_session("S1"),),
        )
    )


def _duration_not_expressible() -> ProblemInstance:
    """A 45-minute class on a 60-minute grid (ADR-0006)."""
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(_room(),),
            teachers=(_teacher(),),
            sessions=(_session("S1", contact_minutes=45),),
        )
    )


def _asynchronous_session_ignored() -> ProblemInstance:
    """A session with ``requires_timeslot = False`` generates no variable and
    must not block publication (ADR-0004)."""
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 1),
            rooms=(_room(),),
            teachers=(_teacher(),),
            sessions=(
                _session("S1"),
                _session(
                    "S2-async", requires_timeslot=False, requires_room=False
                ),
            ),
        )
    )


def _downgraded_capacity_admits_small_room() -> ProblemInstance:
    """A named human downgraded ROOM_CAPACITY, so it stops pruning.

    Without the override this is BLOCKED — no room is large enough. With it,
    the constraint becomes a tier-1 objective term and the instance solves,
    over capacity, exactly as the institution decided. Getting this backwards
    in either direction is a serious bug: pruning anyway silently ignores a
    recorded decision; not moving it into tier 1 silently stops minimising it.
    """
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=_slots(_MON, 2),
            rooms=(_room(capacity=10),),
            teachers=(_teacher(),),
            sessions=(_session("S1", seats=60),),
        ),
        constraints=(
            EffectiveConstraint(
                code="ROOM_CAPACITY",
                effective_category=ConstraintCategory.MEDIUM,
                overridden_by_user_id="U-dean",
                override_reason="Acuerdo de facultad 2026-03: aforo +20% temporal",
            ),
        ),
    )


def _minimal_disruption_base() -> ProblemInstance:
    """A published schedule to repair, for SESSION 4 and SESSION 11.

    S1 is locked at Monday 08:00; S2 sits at 09:00 and may move. Any
    reoptimisation that moves S1 has violated ASSIGNMENT_LOCKED; any that
    moves S2 without cause has paid stability for nothing.
    """
    return _instance(
        BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON, _TUE)),
            slots=_slots(_MON, 3) + _slots(_TUE, 3),
            rooms=(_room("R1"), _room("R2")),
            teachers=(_teacher("T1"), _teacher("T2")),
            sessions=(_session("S1"), _session("S2", teachers=("T2",))),
            base_assignments=(
                BaseAssignment(
                    session_id="S1", start_slot_id="mon0", room_id="R1", is_locked=True
                ),
                BaseAssignment(session_id="S2", start_slot_id="mon1", room_id="R1"),
            ),
        ),
        profile="MINIMAL_DISRUPTION",
    )


_ALL: tuple[Fixture, ...] = (
    Fixture(
        code="TRIVIAL_FEASIBLE",
        description="One session, one room, two slots. The sanity check.",
        expectation=Expectation.FEASIBLE,
        build=_trivial_feasible,
        isolates=("SESSION_WITHIN_DAY_GRID",),
    ),
    Fixture(
        code="TEACHER_CLASH_FORCED",
        description=(
            "Two sessions share a teacher and the week has one slot. No "
            "schedule exists and the solver must prove it."
        ),
        expectation=Expectation.INFEASIBLE,
        build=_teacher_clash_forced,
        isolates=("TEACHER_NO_OVERLAP",),
    ),
    Fixture(
        code="TEACHER_CLASH_AVOIDABLE",
        description=(
            "The same teacher with two slots and one room: both sessions fit, "
            "one per slot."
        ),
        expectation=Expectation.FEASIBLE,
        build=_teacher_clash_avoidable,
        isolates=("TEACHER_NO_OVERLAP", "ROOM_NO_OVERLAP"),
    ),
    Fixture(
        code="ROOM_CONTENTION_FORCED",
        description="Two teachers, one room, one slot.",
        expectation=Expectation.INFEASIBLE,
        build=_room_contention_forced,
        isolates=("ROOM_NO_OVERLAP",),
    ),
    Fixture(
        code="LUNCH_BAND_BLOCKS_LONG_SESSION",
        description=(
            "A 3-slot session cannot start at 08:00 because 10:00 is blocked, "
            "and cannot start later without running past the end of the day."
        ),
        expectation=Expectation.BLOCKED,
        build=_lunch_band_blocks_long_session,
        isolates=("SESSION_SLOTS_SCHEDULABLE", "SESSION_WITHIN_DAY_GRID"),
        expected_diagnostics=(DiagnosticCode.SESSION_HAS_NO_FEASIBLE_START,),
    ),
    Fixture(
        code="SESSION_LONGER_THAN_DAY",
        description="A 3-slot session in a 2-slot day.",
        expectation=Expectation.BLOCKED,
        build=_session_longer_than_day,
        isolates=("SESSION_WITHIN_DAY_GRID",),
        expected_diagnostics=(
            DiagnosticCode.SESSION_LONGER_THAN_ANY_DAY,
            DiagnosticCode.SESSION_HAS_NO_FEASIBLE_START,
        ),
    ),
    Fixture(
        code="NO_ROOM_LARGE_ENOUGH",
        description="60 students, one room seating 10.",
        expectation=Expectation.BLOCKED,
        build=_no_room_large_enough,
        isolates=("ROOM_CAPACITY",),
        expected_diagnostics=(DiagnosticCode.SESSION_HAS_NO_FEASIBLE_ROOM,),
    ),
    Fixture(
        code="NO_ROOM_WITH_FEATURE",
        description="The session needs 4 fume hoods; the only room has none.",
        expectation=Expectation.BLOCKED,
        build=_no_room_with_feature,
        isolates=("ROOM_FEATURE_REQUIREMENT",),
        expected_diagnostics=(DiagnosticCode.SESSION_HAS_NO_FEASIBLE_ROOM,),
    ),
    Fixture(
        code="TEACHER_UNAVAILABLE_EVERYWHERE",
        description=(
            "The only teacher is unavailable in every slot of the grid, so the "
            "session's start domain is empty before the solver is built."
        ),
        expectation=Expectation.BLOCKED,
        build=_teacher_unavailable_everywhere,
        isolates=("TEACHER_AVAILABILITY",),
        expected_diagnostics=(DiagnosticCode.SESSION_HAS_NO_FEASIBLE_START,),
    ),
    Fixture(
        code="STUDENT_CONFLICT_UNRESOLVABLE",
        description=(
            "One student, two mandatory single-section courses, one slot. "
            "Neither enrollment can be re-sectioned, so the clash is HARD and "
            "the instance is infeasible."
        ),
        expectation=Expectation.INFEASIBLE,
        build=_student_conflict_unresolvable,
        isolates=("STUDENT_NO_OVERLAP_UNRESOLVABLE",),
    ),
    Fixture(
        code="STUDENT_CONFLICT_AVOIDABLE",
        description="The same two mandatory courses, with two slots available.",
        expectation=Expectation.FEASIBLE,
        build=_student_conflict_avoidable,
        isolates=("STUDENT_NO_OVERLAP_UNRESOLVABLE",),
    ),
    Fixture(
        code="STUDENT_CONFLICT_ELECTIVE_TOLERATED",
        description=(
            "One slot and a clash the student can resolve by dropping an "
            "elective. Feasible: the clash is an objective term. INFEASIBLE "
            "here would mean every student conflict had been promoted to HARD."
        ),
        expectation=Expectation.FEASIBLE,
        build=_student_conflict_elective_tolerated,
        isolates=("STUDENT_NO_OVERLAP_ELECTIVE",),
    ),
    Fixture(
        code="CO_TEACHING_MERGED",
        description=(
            "Two offerings taught together must share a slot and a room, and "
            "the room must seat both sections."
        ),
        expectation=Expectation.FEASIBLE,
        build=_co_teaching_merged,
        isolates=("CO_TEACHING_SAME_TIMESLOT", "CO_TEACHING_SAME_ROOM"),
    ),
    Fixture(
        code="CO_TEACHING_UNAPPROVED",
        description="A merge nobody approved must never be performed.",
        expectation=Expectation.BLOCKED,
        build=_co_teaching_unapproved,
        isolates=("CO_TEACHING_SAME_TIMESLOT",),
        expected_diagnostics=(DiagnosticCode.CO_TEACHING_GROUP_NOT_APPROVED,),
    ),
    Fixture(
        code="AVAILABILITY_UNDECLARED",
        description=(
            "Solves cleanly and is quietly wrong: nobody recorded this "
            "teacher's availability, and an empty deny-list is indistinguishable "
            "from genuine all-week freedom."
        ),
        expectation=Expectation.FEASIBLE,
        build=_availability_undeclared,
        isolates=("TEACHER_AVAILABILITY",),
        expected_diagnostics=(
            DiagnosticCode.TEACHER_AVAILABILITY_UNDECLARED,
            DiagnosticCode.ROOM_AVAILABILITY_UNDECLARED,
        ),
    ),
    Fixture(
        code="DURATION_NOT_EXPRESSIBLE",
        description="A 45-minute class on a 60-minute grid: named, not rounded.",
        expectation=Expectation.FEASIBLE,
        build=_duration_not_expressible,
        isolates=("SESSION_WITHIN_DAY_GRID",),
        expected_diagnostics=(DiagnosticCode.SESSION_DURATION_NOT_EXPRESSIBLE,),
    ),
    Fixture(
        code="ASYNCHRONOUS_SESSION_IGNORED",
        description=(
            "An asynchronous session generates no decision variable and does "
            "not block publication, even though the grid has only one slot."
        ),
        expectation=Expectation.FEASIBLE,
        build=_asynchronous_session_ignored,
        isolates=("SESSION_ROOM_PRESENCE",),
        expected_diagnostics=(DiagnosticCode.SESSION_NOT_SCHEDULED_BY_DESIGN,),
    ),
    Fixture(
        code="DOWNGRADED_CAPACITY_ADMITS_SMALL_ROOM",
        description=(
            "ROOM_CAPACITY downgraded by a named human with a reason: the "
            "domain stops being pruned and the term moves to objective tier 1."
        ),
        expectation=Expectation.FEASIBLE,
        build=_downgraded_capacity_admits_small_room,
        isolates=("ROOM_CAPACITY",),
    ),
    Fixture(
        code="MINIMAL_DISRUPTION_BASE",
        description=(
            "A published two-day schedule with one locked assignment, as the "
            "baseline for SESSION 4 benchmarking and SESSION 11 repair."
        ),
        expectation=Expectation.FEASIBLE,
        build=_minimal_disruption_base,
        isolates=("ASSIGNMENT_LOCKED", "SCHEDULE_STABILITY"),
        objective_profile="MINIMAL_DISRUPTION",
    ),
)


FIXTURES: Mapping[str, Fixture] = MappingProxyType({f.code: f for f in _ALL})


def get_fixture(code: str) -> Fixture:
    try:
        return FIXTURES[code]
    except KeyError:
        raise KeyError(
            f"unknown fixture {code!r}; known: {', '.join(sorted(FIXTURES))}"
        ) from None
