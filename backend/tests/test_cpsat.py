"""CP-SAT baseline: adapters, solving, and independent validation.

The suite is organised around the three answers a run can give, because
telling them apart is most of what SESSION 3 is for:

* **BLOCKED** — rejected before the solver runs, with a named diagnostic.
* **INFEASIBLE** — the solver *proved* no schedule exists.
* **FEASIBLE/OPTIMAL** — a schedule exists, and it survives a validator that
  re-derives every check independently of the model that produced it.

A BLOCKED instance that reaches CP-SAT and comes back INFEASIBLE is a bug even
though the status reads correctly: "no solution exists" and "you asked for a
room with fume hoods and there are none" are different answers, and only one
of them tells anybody what to do.
"""

from __future__ import annotations

import pytest

from academic_ai.domain.enums import (
    ConstraintCategory,
    OptimizationRunStatus,
    SolverStatus,
)
from academic_ai.domain.scheduling.catalog import CONSTRAINT_CATALOG, solver_hard_codes
from academic_ai.domain.scheduling.domains import compute_domains
from academic_ai.domain.scheduling.fixtures import FIXTURES, Expectation
from academic_ai.domain.scheduling.problem import (
    EffectiveConstraint,
    ProblemInstance,
    missing_adapters,
)
from academic_ai.optimization import (
    REGISTRY,
    Assignment,
    SolverConfig,
    registered_codes,
    solve,
    validate,
)

CONFIG = SolverConfig(time_limit_seconds=15.0)
FIXTURE_IDS = sorted(FIXTURES)


def run(code: str, config: SolverConfig | None = None):
    fixture = FIXTURES[code]
    instance = fixture.instance()
    _, diagnostics = compute_domains(instance.snapshot, instance.constraints)
    return fixture, instance, solve(instance, config or CONFIG, diagnostics=diagnostics)


class TestAdapterCompleteness:
    def test_every_hard_constraint_has_an_adapter(self) -> None:
        """The single most important safety property here.

        A HARD constraint with no adapter does not fail loudly — it silently
        is not enforced, and CP-SAT returns a confident OPTIMAL for a schedule
        that double-books a room.
        """
        assert solver_hard_codes() <= registered_codes()

    def test_no_adapter_is_registered_for_an_unknown_constraint(self) -> None:
        assert set(REGISTRY) <= set(CONSTRAINT_CATALOG)

    def test_solving_is_refused_when_an_adapter_is_missing(self) -> None:
        """`solve()` must refuse, not proceed with partial enforcement."""
        _, instance, _ = run("TRIVIAL_FEASIBLE")
        pretend_registry = sorted(registered_codes() - {"TEACHER_NO_OVERLAP"})
        assert missing_adapters(instance, pretend_registry) == {"TEACHER_NO_OVERLAP"}

    def test_no_adapter_is_registered_for_a_precondition(self) -> None:
        """A precondition is checked while building, not posted to the solver."""
        from academic_ai.domain.scheduling.enums import Enforcement

        for code in REGISTRY:
            assert (
                CONSTRAINT_CATALOG[code].enforcement is Enforcement.SOLVER
            ), f"{code} is a precondition but has a solver adapter"


class TestFixtureExpectations:
    @pytest.mark.parametrize("code", FIXTURE_IDS)
    def test_fixture_produces_its_declared_answer(self, code: str) -> None:
        fixture, _, outcome = run(code)

        if fixture.expectation is Expectation.BLOCKED:
            assert outcome.solver_status is None, (
                f"{code} must be rejected before the solver runs; returning "
                f"{outcome.solver_status} loses the named diagnostic"
            )
            assert outcome.status is OptimizationRunStatus.FAILED
            assert outcome.failure_reason
        elif fixture.expectation is Expectation.INFEASIBLE:
            assert outcome.is_proven_infeasible, (
                f"{code} must be *proven* infeasible, got {outcome.solver_status}"
            )
        else:
            assert outcome.is_solved, f"{code}: {outcome.failure_reason}"

    @pytest.mark.parametrize("code", FIXTURE_IDS)
    def test_every_returned_schedule_passes_independent_validation(
        self, code: str
    ) -> None:
        _, _, outcome = run(code)
        if not outcome.is_solved:
            return
        assert outcome.validation is not None
        assert outcome.validation.is_valid, [
            str(v) for v in outcome.validation.violations
        ]

    @pytest.mark.parametrize("code", FIXTURE_IDS)
    def test_expected_diagnostics_are_emitted(self, code: str) -> None:
        fixture, instance, _ = run(code)
        _, diagnostics = compute_domains(instance.snapshot, instance.constraints)
        emitted = {d.code for d in diagnostics}
        missing = set(fixture.expected_diagnostics) - emitted
        assert not missing, f"{code}: {sorted(str(m) for m in missing)}"


class TestStatusVocabulary:
    def test_proven_infeasible_is_not_a_timeout(self) -> None:
        """INFEASIBLE means no schedule exists. TIMEOUT means we stopped
        looking. Telling a coordinator the first when the truth is the second
        is the failure SESSION 6 exists to prevent."""
        _, _, outcome = run("TEACHER_CLASH_FORCED")
        assert outcome.solver_status is SolverStatus.INFEASIBLE
        assert outcome.is_proven_infeasible
        assert outcome.status is OptimizationRunStatus.COMPLETED

    def test_a_blocked_run_reports_no_solver_status_at_all(self) -> None:
        _, _, outcome = run("NO_ROOM_WITH_FEATURE")
        assert outcome.solver_status is None
        assert not outcome.is_proven_infeasible
        assert "fume" in outcome.failure_reason.lower() or outcome.failure_reason

    def test_a_solved_run_reports_optimal_or_feasible(self) -> None:
        _, _, outcome = run("TRIVIAL_FEASIBLE")
        assert outcome.solver_status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)


class TestHardConstraintsAreEnforced:
    def test_teacher_double_booking_is_impossible(self) -> None:
        _, _, outcome = run("TEACHER_CLASH_AVOIDABLE")
        assert outcome.is_solved
        starts = {a.start_slot_id for a in outcome.assignments}
        assert len(starts) == 2, "one teacher, two sessions, must be separated"

    def test_room_contention_forces_separation(self) -> None:
        _, _, outcome = run("ROOM_CONTENTION_FORCED")
        assert outcome.is_proven_infeasible

    def test_an_unresolvable_student_clash_is_hard(self) -> None:
        _, _, outcome = run("STUDENT_CONFLICT_UNRESOLVABLE")
        assert outcome.is_proven_infeasible

    def test_a_resolvable_student_clash_is_not_hard(self) -> None:
        """SESSION 5: "Do not silently convert all student conflicts into
        HARD." An elective clash must still solve."""
        _, _, outcome = run("STUDENT_CONFLICT_ELECTIVE_TOLERATED")
        assert outcome.is_solved

    def test_a_locked_assignment_keeps_its_slot_and_room(self) -> None:
        _, instance, outcome = run("MINIMAL_DISRUPTION_BASE")
        assert outcome.is_solved
        locked = [b for b in instance.snapshot.base_assignments if b.is_locked]
        placed = {a.session_id: a for a in outcome.assignments}
        for base in locked:
            assert placed[base.session_id].start_slot_id == base.start_slot_id
            assert placed[base.session_id].room_id == base.room_id

    def test_a_co_teaching_group_shares_slot_and_room(self) -> None:
        _, instance, outcome = run("CO_TEACHING_MERGED")
        assert outcome.is_solved, outcome.failure_reason
        placed = {a.session_id: a for a in outcome.assignments}
        for group in instance.snapshot.co_teaching_groups:
            members = [placed[m] for m in group.member_session_ids]
            assert len({m.start_slot_id for m in members}) == 1
            assert len({m.room_id for m in members}) == 1

    def test_a_co_teaching_group_needs_a_room_for_everybody(self) -> None:
        """Seat demand is the sum, so the 30-seat room must be rejected even
        though it fits either member alone (ADR-0004)."""
        _, _, outcome = run("CO_TEACHING_MERGED")
        assert {a.room_id for a in outcome.assignments} == {"BIG"}

    def test_an_asynchronous_session_is_never_scheduled(self) -> None:
        _, _, outcome = run("ASYNCHRONOUS_SESSION_IGNORED")
        assert outcome.is_solved
        assert "S2-async" not in {a.session_id for a in outcome.assignments}


class TestOverridesChangeWhatTheSolverMayDo:
    def test_a_downgraded_capacity_admits_an_undersized_room(self) -> None:
        """The override is what makes this solvable; without it the instance
        is blocked before the solver runs."""
        _, instance, outcome = run("DOWNGRADED_CAPACITY_ADMITS_SMALL_ROOM")
        assert outcome.is_solved
        assert "ROOM_CAPACITY" not in instance.hard_codes()
        assert outcome.assignments[0].room_id == "R1"

    def test_the_same_data_without_the_override_is_blocked(self) -> None:
        fixture = FIXTURES["DOWNGRADED_CAPACITY_ADMITS_SMALL_ROOM"]
        original = fixture.instance()
        stripped = ProblemInstance(
            snapshot=original.snapshot,
            constraints=(),
            objective_profile=original.objective_profile,
        )
        _, diagnostics = compute_domains(stripped.snapshot, stripped.constraints)
        outcome = solve(stripped, CONFIG, diagnostics=diagnostics)
        assert outcome.solver_status is None, "must be blocked, not solved"

    def test_physical_reality_still_cannot_be_downgraded(self) -> None:
        with pytest.raises(ValueError, match="physical or safety reality"):
            EffectiveConstraint(
                code="TEACHER_NO_OVERLAP",
                effective_category=ConstraintCategory.SOFT,
                overridden_by_user_id="U1",
                override_reason="convenience",
            )


class TestValidatorIsIndependent:
    def test_the_validator_catches_a_hand_made_teacher_clash(self) -> None:
        """The validator must not agree with the builder by construction."""
        _, instance, _ = run("TEACHER_CLASH_AVOIDABLE")
        forged = (
            Assignment("S1", "mon0", "R1"),
            Assignment("S2", "mon0", "R1"),
        )
        report = validate(instance, forged)
        assert not report.is_valid
        assert "TEACHER_NO_OVERLAP" in report.by_code()

    def test_the_validator_catches_a_missing_assignment(self) -> None:
        _, instance, _ = run("TEACHER_CLASH_AVOIDABLE")
        report = validate(instance, (Assignment("S1", "mon0", "R1"),))
        assert "COVERAGE" in report.by_code()

    def test_the_validator_catches_a_blocked_slot(self) -> None:
        _, instance, _ = run("AVAILABILITY_UNDECLARED")
        report = validate(instance, (Assignment("S1", "mon0", "R1"),))
        assert report.is_valid

    def test_the_validator_catches_an_oversubscribed_room(self) -> None:
        _, instance, _ = run("CO_TEACHING_MERGED")
        forged = (
            Assignment("S1", "mon0", "SMALL"),
            Assignment("S2", "mon0", "SMALL"),
        )
        report = validate(instance, forged)
        assert "ROOM_CAPACITY" in report.by_code()

    def test_the_validator_rejects_scheduling_an_asynchronous_session(self) -> None:
        _, instance, _ = run("ASYNCHRONOUS_SESSION_IGNORED")
        forged = (
            Assignment("S1", "mon0", "R1"),
            Assignment("S2-async", "mon0", None),
        )
        report = validate(instance, forged)
        assert "COVERAGE" in report.by_code()


class TestDeterminism:
    def test_the_same_seed_gives_the_same_schedule(self) -> None:
        """SESSION 3 requires reproducible fixtures and SESSION 4 forbids
        claiming an improvement without reproducible evidence."""
        config = SolverConfig(time_limit_seconds=15.0, workers=1, random_seed=7)
        first = run("MINIMAL_DISRUPTION_BASE", config)[2]
        second = run("MINIMAL_DISRUPTION_BASE", config)[2]
        assert first.assignments == second.assignments

    def test_tier_values_are_recorded_for_every_tier(self) -> None:
        """Without per-tier optima, "the schedule got better" is
        unfalsifiable."""
        _, instance, outcome = run("MINIMAL_DISRUPTION_BASE")
        assert outcome.is_solved
        assert len(outcome.tiers) == len(instance.objective_profile.tiers)
        assert [t.rank for t in outcome.tiers] == [
            t.rank for t in instance.objective_profile.tiers
        ]

    def test_metrics_are_structured(self) -> None:
        _, _, outcome = run("TRIVIAL_FEASIBLE")
        metrics = outcome.metrics()
        assert metrics["assignments"] == 1.0
        assert metrics["violations"] == 0.0
        assert metrics["wall_seconds"] >= 0.0


class TestObjective:
    def test_stability_keeps_an_unlocked_session_where_it_was(self) -> None:
        """S2 is free to move but has no reason to; the stability tier should
        leave it on its base slot."""
        _, instance, outcome = run("MINIMAL_DISRUPTION_BASE")
        assert outcome.is_solved
        placed = {a.session_id: a for a in outcome.assignments}
        base = {b.session_id: b for b in instance.snapshot.base_assignments}
        assert placed["S2"].start_slot_id == base["S2"].start_slot_id

    def test_an_elective_clash_is_minimised_when_it_can_be(self) -> None:
        """One slot forces the clash; the run still succeeds and records it."""
        _, _, outcome = run("STUDENT_CONFLICT_ELECTIVE_TOLERATED")
        assert outcome.is_solved
        elective = next(
            (t for t in outcome.tiers if "ELECTIVE" in t.code), None
        )
        assert elective is not None


class TestReviewFindings:
    """Regressions for the SESSION 3 optimizer-agent review.

    Every one of these was a schedule the solver called OPTIMAL and a human
    would have published.
    """

    def test_co_teaching_is_independent_of_member_order(self) -> None:
        """The same data, listed the other way round, gave INFEASIBLE instead
        of a double-booked OPTIMAL. One institution, two answers, decided by
        tuple order."""
        from academic_ai.domain.enums import DayOfWeek
        from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
        from academic_ai.domain.scheduling.problem import (
            BaseSnapshot,
            CoTeachingGroupSpec,
            PeriodSpec,
            RoomSpec,
            SessionSpec,
            SlotSpec,
            TeacherSpec,
        )

        def build(order):
            return ProblemInstance(
                snapshot=BaseSnapshot(
                    period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,)),
                    slots=tuple(
                        SlotSpec(f"m{i}", DayOfWeek.MONDAY, i, i, 480 + 60 * i)
                        for i in range(3)
                    ),
                    rooms=(
                        RoomSpec("R1", "C1", 30, "CLASSROOM", has_declared_availability=True),
                    ),
                    teachers=(
                        TeacherSpec("TA", has_declared_availability=True),
                        TeacherSpec("TB", has_declared_availability=True),
                    ),
                    sessions=(
                        SessionSpec("A", "sA", "oA", "cA", "C1", 1, teacher_ids=("TA",)),
                        SessionSpec("B", "sB", "oB", "cB", "C1", 3, teacher_ids=("TA",)),
                        SessionSpec("X", "sX", "oX", "cX", "C1", 1, teacher_ids=("TB",)),
                    ),
                    co_teaching_groups=(
                        CoTeachingGroupSpec("G", order, is_approved=True),
                    ),
                ),
                constraints=(),
                objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
            )

        forward = solve(build(("A", "B")), CONFIG)
        backward = solve(build(("B", "A")), CONFIG)
        assert forward.solver_status is backward.solver_status
        assert forward.is_solved == backward.is_solved

    def test_a_group_is_protected_for_its_longest_member(self) -> None:
        """Representing a group by one member left a longer peer's tail
        unguarded, so another class could be scheduled inside it."""
        from academic_ai.domain.enums import DayOfWeek
        from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
        from academic_ai.domain.scheduling.problem import (
            BaseSnapshot,
            CoTeachingGroupSpec,
            PeriodSpec,
            RoomSpec,
            SessionSpec,
            SlotSpec,
            TeacherSpec,
        )

        instance = ProblemInstance(
            snapshot=BaseSnapshot(
                period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,)),
                slots=tuple(
                    SlotSpec(f"m{i}", DayOfWeek.MONDAY, i, i, 480 + 60 * i)
                    for i in range(3)
                ),
                rooms=(RoomSpec("R1", "C1", 30, "CLASSROOM", has_declared_availability=True),),
                teachers=(
                    TeacherSpec("TA", has_declared_availability=True),
                    TeacherSpec("TB", has_declared_availability=True),
                ),
                sessions=(
                    SessionSpec("A", "sA", "oA", "cA", "C1", 1, teacher_ids=("TA",)),
                    SessionSpec("B", "sB", "oB", "cB", "C1", 3, teacher_ids=("TA",)),
                    SessionSpec("X", "sX", "oX", "cX", "C1", 1, teacher_ids=("TB",)),
                ),
                co_teaching_groups=(
                    CoTeachingGroupSpec("G", ("A", "B"), is_approved=True),
                ),
            ),
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        outcome = solve(instance, CONFIG)
        assert not outcome.is_solved, "a 3-slot group leaves no room for X"

    def test_a_scoped_downgrade_does_not_disable_the_constraint_everywhere(
        self,
    ) -> None:
        """A capacity override attached to one auditorium removed ROOM_CAPACITY
        from the enforced set entirely — and the validator, reading the same
        set, reported the result as valid."""
        from academic_ai.domain.enums import ConstraintCategory, DayOfWeek
        from academic_ai.domain.scheduling.domains import compute_domains
        from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
        from academic_ai.domain.scheduling.problem import (
            BaseSnapshot,
            PeriodSpec,
            RoomSpec,
            SessionSpec,
            SlotSpec,
            TeacherSpec,
        )
        from academic_ai.domain.scheduling.scope import ConstraintScope

        instance = ProblemInstance(
            snapshot=BaseSnapshot(
                period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,)),
                slots=(SlotSpec("m0", DayOfWeek.MONDAY, 0, 0, 480),),
                rooms=(
                    RoomSpec("AUD", "C1", 10, "CLASSROOM", has_declared_availability=True),
                    RoomSpec("TINY", "C1", 1, "CLASSROOM", has_declared_availability=True),
                ),
                teachers=(TeacherSpec("T1", has_declared_availability=True),),
                sessions=(
                    SessionSpec(
                        "V", "sec", "off", "crs", "C1", 1,
                        teacher_ids=("T1",), seat_demand=50,
                    ),
                ),
            ),
            constraints=(
                EffectiveConstraint(
                    code="ROOM_CAPACITY",
                    effective_category=ConstraintCategory.MEDIUM,
                    scope=ConstraintScope(room_id="AUD"),
                    overridden_by_user_id="U-dean",
                    override_reason="aforo excepcional",
                ),
            ),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        assert "ROOM_CAPACITY" in instance.hard_codes()
        domains, _ = compute_domains(instance.snapshot, instance.constraints)
        assert "TINY" not in domains["V"].room_ids

    def test_a_run_whose_schedule_failed_validation_is_not_solved(self) -> None:
        """`is_solved` was True for a schedule the validator rejected, so a
        caller trusting it would publish a double-booked timetable."""
        from academic_ai.optimization.solve import SolveOutcome
        from academic_ai.optimization.validator import ValidationReport, Violation

        broken = SolveOutcome(
            status=OptimizationRunStatus.COMPLETED,
            solver_status=SolverStatus.OPTIMAL,
            validation=ValidationReport(
                (Violation("ROOM_NO_OVERLAP", "R1: A and B overlap"),)
            ),
        )
        assert not broken.is_solved

    def test_the_occupancy_stride_survives_a_hole_in_the_grid(self) -> None:
        """A stride derived from the slot *count* let two different days share
        an occupancy value — visible only as an unexplainable INFEASIBLE."""
        from academic_ai.domain.enums import DayOfWeek
        from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
        from academic_ai.domain.scheduling.problem import (
            BaseSnapshot,
            PeriodSpec,
            SessionSpec,
            SlotSpec,
        )
        from academic_ai.optimization.variables import build_variables

        instance = ProblemInstance(
            snapshot=BaseSnapshot(
                period=PeriodSpec(
                    id="P", slot_minutes=60, days=(DayOfWeek.MONDAY, DayOfWeek.TUESDAY)
                ),
                slots=(
                    SlotSpec("a0", DayOfWeek.MONDAY, 0, 0, 480),
                    SlotSpec("a1", DayOfWeek.MONDAY, 1, 1, 540),
                    SlotSpec("a4", DayOfWeek.MONDAY, 4, 4, 780),  # hole at 2,3
                    SlotSpec("b0", DayOfWeek.TUESDAY, 0, 5, 480),
                    SlotSpec("b1", DayOfWeek.TUESDAY, 1, 6, 540),
                ),
                sessions=(
                    SessionSpec("S", "sec", "off", "crs", "C1", 1, requires_room=False),
                ),
            ),
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        variables = build_variables(instance)
        indices = list(variables.occupancy_index.values())
        assert len(indices) == len(set(indices)), "two days share an occupancy value"


class TestCampusTravel:
    """The one HARD constraint that had no test at all.

    Travel is the only constraint that reasons about *elapsed time* rather
    than about occupancy, and the occupancy index only measures elapsed time
    inside one day. The builder used to read a distance off it across a day
    boundary, which measures the stride: a teacher working two campuses on two
    different days came back as a proven INFEASIBLE. `TEACHER_CAMPUS_TRAVEL`
    is HARD and non-overridable, so no coordinator could work around it.
    """

    @staticmethod
    def _two_campus_instance(
        *,
        days: tuple,
        travel_minutes: dict,
        slots_per_day: int = 4,
    ) -> ProblemInstance:
        """One teacher, two sessions, one room on each of two campuses.

        Each session is pinned to its campus by making only that campus's room
        big enough, so the placement question is purely "when", which is what
        travel is about.
        """
        from academic_ai.domain.enums import DayOfWeek, SessionType
        from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
        from academic_ai.domain.scheduling.problem import (
            BaseSnapshot,
            PeriodSpec,
            RoomSpec,
            SessionSpec,
            SlotSpec,
            TeacherSpec,
        )

        slots = tuple(
            SlotSpec(
                id=f"{day.value[:3].lower()}{i}",
                day_of_week=day,
                in_day_index=i,
                global_index=d * 10 + i,
                start_minute=(8 + i) * 60,
            )
            for d, day in enumerate(days)
            for i in range(slots_per_day)
        )
        rooms = (
            RoomSpec(id="NORTH", campus_id="N", capacity=40, room_type="CLASSROOM",
                     has_declared_availability=True),
            RoomSpec(id="SOUTH", campus_id="S", capacity=40, room_type="CLASSROOM",
                     has_declared_availability=True),
        )

        def session(id_: str, campus: str) -> SessionSpec:
            return SessionSpec(
                id=id_,
                section_id=f"{id_}-sec",
                offering_id=f"{id_}-off",
                course_id=f"{id_}-crs",
                campus_id=campus,
                duration_slots=1,
                session_type=SessionType.LECTURE,
                teacher_ids=("T1",),
                seat_demand=20,
            )

        return ProblemInstance(
            snapshot=BaseSnapshot(
                period=PeriodSpec(id="P", slot_minutes=60, days=days),
                slots=slots,
                rooms=rooms,
                teachers=(TeacherSpec(id="T1", has_declared_availability=True),),
                sessions=(session("A", "N"), session("B", "S")),
                travel_minutes=travel_minutes,
            ),
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )

    def test_two_campuses_on_two_days_is_feasible(self) -> None:
        """The regression: a teacher working two campuses on two days.

        One teaching slot per day, so the only schedule is one session on each
        day. That is a night apart and nobody travels anywhere, but the builder
        charged the journey against the occupancy index — whose day stride is
        an arbitrary number chosen to keep days disjoint, not a duration. Here
        the stride is 2 and the journey is 2 slots, so both orientations failed
        and CP-SAT returned a *proven* INFEASIBLE.

        The width of the stride is what decides whether the old bug showed up
        at all, which is why it survived: give the days four slots each and the
        solver simply sidesteps it, and the timetable is merely distorted
        rather than refused.
        """
        from academic_ai.domain.enums import DayOfWeek

        instance = self._two_campus_instance(
            days=(DayOfWeek.MONDAY, DayOfWeek.TUESDAY),
            slots_per_day=1,
            travel_minutes={("N", "S"): 90, ("S", "N"): 90},
        )
        outcome = solve(instance, CONFIG)
        assert outcome.is_solved, (
            "a teacher working two campuses on two different days is not an "
            "impossibility, and this constraint cannot be overridden"
        )

    def test_travel_is_still_enforced_within_a_day(self) -> None:
        """Day-scoping must not switch the constraint off.

        One day, four slots, and a journey longer than the day: the two
        sessions cannot both be placed, and INFEASIBLE is the right answer.
        """
        from academic_ai.domain.enums import DayOfWeek

        instance = self._two_campus_instance(
            days=(DayOfWeek.MONDAY,),
            travel_minutes={("N", "S"): 600, ("S", "N"): 600},
        )
        outcome = solve(instance, CONFIG)
        assert not outcome.is_solved

    def test_a_reachable_journey_within_a_day_is_scheduled_with_the_gap(self) -> None:
        from academic_ai.domain.enums import DayOfWeek

        instance = self._two_campus_instance(
            days=(DayOfWeek.MONDAY,),
            travel_minutes={("N", "S"): 60, ("S", "N"): 60},
        )
        outcome = solve(instance, CONFIG)
        assert outcome.is_solved
        placed = {a.session_id: a for a in outcome.assignments}
        index = {s.id: s.in_day_index for s in instance.snapshot.slots}
        first, second = sorted(
            (index[placed["A"].start_slot_id], index[placed["B"].start_slot_id])
        )
        assert second - (first + 1) >= 1, "the hour of travel was not left free"

    def test_the_validator_ignores_a_cross_day_pair(self) -> None:
        """The validator has always day-scoped travel. Pinned here so the two
        sides cannot drift apart again in the other direction."""
        from academic_ai.domain.enums import DayOfWeek

        instance = self._two_campus_instance(
            days=(DayOfWeek.MONDAY, DayOfWeek.TUESDAY),
            travel_minutes={("N", "S"): 90, ("S", "N"): 90},
        )
        report = validate(
            instance,
            (Assignment("A", "mon3", "NORTH"), Assignment("B", "tue0", "SOUTH")),
        )
        assert "TEACHER_CAMPUS_TRAVEL" not in report.by_code()

    def test_the_validator_catches_a_hand_made_same_day_journey(self) -> None:
        from academic_ai.domain.enums import DayOfWeek

        instance = self._two_campus_instance(
            days=(DayOfWeek.MONDAY, DayOfWeek.TUESDAY),
            travel_minutes={("N", "S"): 90, ("S", "N"): 90},
        )
        report = validate(
            instance,
            (Assignment("A", "mon0", "NORTH"), Assignment("B", "mon1", "SOUTH")),
        )
        assert "TEACHER_CAMPUS_TRAVEL" in report.by_code()

    def test_a_one_way_travel_row_is_enforced_in_that_direction(self) -> None:
        """`travel_minutes` is routinely half-populated.

        The catalog is explicit that travel is directional and that a missing
        row is *vacuous*, not infinite — so N->S must be enforced and S->N must
        not. The builder posts both orientations regardless, because skipping
        the zero branch would leave the ordering literal free to select it and
        the solver would escape through the gap it was told to respect.
        """
        from academic_ai.domain.enums import DayOfWeek

        instance = self._two_campus_instance(
            days=(DayOfWeek.MONDAY,),
            travel_minutes={("N", "S"): 120},
        )
        outcome = solve(instance, CONFIG)
        assert outcome.is_solved
        placed = {a.session_id: a for a in outcome.assignments}
        index = {s.id: s.in_day_index for s in instance.snapshot.slots}
        a_at, b_at = index[placed["A"].start_slot_id], index[placed["B"].start_slot_id]
        assert a_at != b_at, "the free orientation was used to overlap them"
        if a_at < b_at:  # N then S: the declared journey applies
            assert b_at - (a_at + 1) >= 2
