"""SESSION 10: what a proposed move would do, measured before anybody does it.

The rule ADR-0012 marks CRITICAL is that a drag never modifies the accepted
schedule. `test_the_accepted_placement_is_never_modified` is the assertion
that makes it true of the code rather than of our intentions.

The rest is about the preview being worth waiting for. A preview that only
said "this is allowed" would be a speed bump users learn to click through, so
these tests check that the numbers are real: named conflicts, per-tier deltas,
and counts that include people whose week changed even when nothing broke.
"""

from __future__ import annotations

import pytest

from academic_ai.domain.enums import ConstraintCategory, DayOfWeek
from academic_ai.domain.scheduling.impact import (
    AffectedCounts,
    ProposedMove,
    analyse_impact,
    apply_moves,
)
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
from academic_ai.domain.scheduling.problem import (
    BaseAssignment,
    BaseSnapshot,
    EffectiveConstraint,
    PeriodSpec,
    ProblemInstance,
    RoomSpec,
    SessionSpec,
    SlotSpec,
    StudentEnrollmentSpec,
    TeacherSpec,
)

_MON = DayOfWeek.MONDAY


def _instance(
    *,
    enrollments: tuple[StudentEnrollmentSpec, ...] = (),
    base_assignments: tuple[BaseAssignment, ...] = (),
    teachers_per_session: tuple[tuple[str, ...], ...] = (("T1",), ("T2",)),
    profile: str | None = None,
) -> ProblemInstance:
    # The domain refuses GREENFIELD when base assignments are present, because
    # that profile omits SCHEDULE_STABILITY and would silently ignore them.
    # Picking the profile from the data keeps these fixtures honest.
    profile = profile or ("MINIMAL_DISRUPTION" if base_assignments else "GREENFIELD")
    return ProblemInstance(
        snapshot=BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=tuple(
                SlotSpec(f"m{i}", _MON, i, i, (8 + i) * 60) for i in range(4)
            ),
            rooms=(
                RoomSpec(id="R1", campus_id="C1", capacity=40, room_type="CLASSROOM",
                         has_declared_availability=True),
                RoomSpec(id="R2", campus_id="C1", capacity=40, room_type="CLASSROOM",
                         has_declared_availability=True),
            ),
            teachers=(
                TeacherSpec(id="T1", has_declared_availability=True),
                TeacherSpec(id="T2", has_declared_availability=True),
            ),
            sessions=tuple(
                SessionSpec(
                    id=f"S{i + 1}",
                    section_id=f"sec{i + 1}",
                    offering_id=f"off{i + 1}",
                    course_id=f"crs{i + 1}",
                    campus_id="C1",
                    duration_slots=1,
                    teacher_ids=teachers,
                    seat_demand=20,
                )
                for i, teachers in enumerate(teachers_per_session)
            ),
            enrollments=enrollments,
            base_assignments=base_assignments,
        ),
        constraints=(
            EffectiveConstraint(
                code="STUDENT_NO_OVERLAP_MANDATORY",
                effective_category=ConstraintCategory.MEDIUM,
            ),
        ),
        objective_profile=OBJECTIVE_PROFILES[profile],
    )


def _placement(s1: str = "m0", s2: str = "m2") -> dict[str, PlacedSession]:
    return {
        "S1": PlacedSession("S1", s1, "R1"),
        "S2": PlacedSession("S2", s2, "R2"),
    }


def _clashing_student(student: str = "E1") -> tuple[StudentEnrollmentSpec, ...]:
    return (
        StudentEnrollmentSpec(student, "off1", ("S1",), True, alternative_section_count=1),
        StudentEnrollmentSpec(student, "off2", ("S2",), True, alternative_section_count=1),
    )


# ---------------------------------------------------------------------------
# The CRITICAL rule
# ---------------------------------------------------------------------------


class TestNothingIsModified:
    def test_the_accepted_placement_is_never_modified(self) -> None:
        """ADR-0012's CRITICAL rule, as an assertion.

        A drag produces a proposal. If `analyse_impact` edited the placement it
        was handed, "the drag did not change anything" would depend on nobody
        having kept a reference to it -- which is exactly the kind of thing
        that is true until the day it is not.
        """
        instance = _instance()
        placement = _placement()
        snapshot = {k: (v.start_slot_id, v.room_id) for k, v in placement.items()}

        analyse_impact(instance, placement, [ProposedMove("S2", "m0", "R1")])

        assert {
            k: (v.start_slot_id, v.room_id) for k, v in placement.items()
        } == snapshot

    def test_apply_moves_returns_a_copy(self) -> None:
        placement = _placement()
        moved = apply_moves(placement, [ProposedMove("S1", "m3")])
        assert moved is not placement
        assert moved["S1"].start_slot_id == "m3"
        assert placement["S1"].start_slot_id == "m0"

    def test_a_time_only_move_keeps_the_room(self) -> None:
        """`to_room_id=None` means "leave the room alone". A drag along the
        time axis must not silently unassign a room."""
        moved = apply_moves(_placement(), [ProposedMove("S1", "m3")])
        assert moved["S1"].room_id == "R1"

    def test_moving_an_unplaced_session_is_ignored_not_invented(self) -> None:
        moved = apply_moves(_placement(), [ProposedMove("S99", "m1")])
        assert "S99" not in moved


# ---------------------------------------------------------------------------
# The preview has to be worth waiting for
# ---------------------------------------------------------------------------


class TestConflictsAreNamed:
    def test_a_move_that_strands_a_student_names_them(self) -> None:
        """"3 new conflicts" tells a coordinator to investigate. "Ana can no
        longer attend Cálculo II" tells them what to do."""
        instance = _instance(enrollments=_clashing_student("ana"))
        report = analyse_impact(
            instance, _placement(), [ProposedMove("S2", "m0")]
        )
        assert len(report.new_conflicts) == 1
        conflict = report.new_conflicts[0]
        assert conflict.student_id == "ana"
        assert {conflict.session_a, conflict.session_b} == {"S1", "S2"}

    def test_a_move_that_frees_a_student_reports_it_resolved(self) -> None:
        instance = _instance(enrollments=_clashing_student("ana"))
        clashing = _placement(s1="m0", s2="m0")
        report = analyse_impact(instance, clashing, [ProposedMove("S2", "m2")])
        assert len(report.resolved_conflicts) == 1
        assert not report.new_conflicts
        assert "resolves 1" in report.summary()

    def test_a_harmless_move_says_so_rather_than_staying_silent(self) -> None:
        """Every drop is analysed, including the obviously fine ones. A user
        who gets no preview for a "safe" move learns the preview is optional."""
        instance = _instance()
        report = analyse_impact(instance, _placement(), [ProposedMove("S1", "m1")])
        assert report.is_neutral
        assert report.summary() == "no measured effect on students or the objective"

    def test_an_unresolvable_conflict_is_flagged_as_such(self) -> None:
        stranded = (
            StudentEnrollmentSpec("ana", "off1", ("S1",), True),
            StudentEnrollmentSpec("ana", "off2", ("S2",), True),
        )
        instance = _instance(enrollments=stranded)
        report = analyse_impact(instance, _placement(), [ProposedMove("S2", "m0")])
        assert report.new_conflicts
        assert any(c.is_unresolvable for c in report.new_conflicts)
        assert "unresolvable" in report.summary()


class TestOccupancyWarnings:
    def test_two_sessions_of_one_teacher_at_one_time_are_flagged(self) -> None:
        instance = _instance(teachers_per_session=(("T1",), ("T1",)))
        report = analyse_impact(instance, _placement(), [ProposedMove("S2", "m0")])
        assert set(report.occupancy_clashes) == {"S1", "S2"}

    def test_two_sessions_in_one_room_at_one_time_are_flagged(self) -> None:
        instance = _instance()
        report = analyse_impact(
            instance, _placement(), [ProposedMove("S2", "m0", "R1")]
        )
        assert set(report.occupancy_clashes) == {"S1", "S2"}

    def test_different_teachers_and_rooms_do_not_clash(self) -> None:
        instance = _instance()
        report = analyse_impact(instance, _placement(), [ProposedMove("S2", "m0")])
        assert report.occupancy_clashes == ()

    def test_a_clash_is_a_warning_not_a_feasibility_verdict(self) -> None:
        """CP-SAT owns feasibility. This exists so the preview can say "two
        classes in Room A at once" rather than letting a coordinator discover
        it after approval."""
        instance = _instance()
        report = analyse_impact(
            instance, _placement(), [ProposedMove("S2", "m0", "R1")]
        )
        assert hasattr(report, "occupancy_clashes")
        assert not hasattr(report, "is_feasible")


class TestScoreDelta:
    def test_the_delta_is_per_tier_never_a_single_number(self) -> None:
        """A scalar reintroduces the cross-tier exchange rate ADR-0010
        rejects, and "+3" for a move that strands one student while pleasing
        forty is worse than no number at all."""
        instance = _instance(enrollments=_clashing_student())
        report = analyse_impact(instance, _placement(), [ProposedMove("S2", "m0")])
        assert isinstance(report.score_delta, tuple)
        assert len(report.score_delta) == len(report.tier_codes)
        assert len(report.tier_codes) > 1

    def test_a_move_that_creates_a_conflict_scores_worse(self) -> None:
        instance = _instance(enrollments=_clashing_student())
        report = analyse_impact(instance, _placement(), [ProposedMove("S2", "m0")])
        assert not report.is_improvement
        assert any(d > 0 for d in report.score_delta)

    def test_a_move_that_resolves_a_conflict_scores_better(self) -> None:
        instance = _instance(enrollments=_clashing_student())
        report = analyse_impact(
            instance, _placement(s1="m0", s2="m0"), [ProposedMove("S2", "m2")]
        )
        assert report.is_improvement

    def test_improvement_is_lexicographic(self) -> None:
        from academic_ai.domain.scheduling.impact import ImpactReport

        better_top = ImpactReport(moves=(), affected=AffectedCounts(),
                                  score_delta=(-1, 500))
        worse_top = ImpactReport(moves=(), affected=AffectedCounts(),
                                 score_delta=(1, -500))
        assert better_top.is_improvement
        assert not worse_top.is_improvement

    def test_named_delta_pairs_tiers_with_values(self) -> None:
        instance = _instance(enrollments=_clashing_student())
        report = analyse_impact(instance, _placement(), [ProposedMove("S2", "m0")])
        named = report.named_delta()
        assert set(named) == set(report.tier_codes)


class TestAffectedCounts:
    def test_a_student_in_a_moved_session_is_affected_even_if_nothing_broke(
        self,
    ) -> None:
        """Their week changed. Counting only conflicts would report zero for a
        move that relocates a class every one of them attends."""
        enrollments = (
            StudentEnrollmentSpec("ana", "off1", ("S1",), True,
                                  alternative_section_count=1),
        )
        instance = _instance(enrollments=enrollments)
        report = analyse_impact(instance, _placement(), [ProposedMove("S1", "m1")])
        assert report.affected.students == 1
        assert report.new_conflicts == ()

    def test_the_moved_sessions_teachers_and_rooms_are_counted(self) -> None:
        instance = _instance()
        report = analyse_impact(
            instance, _placement(), [ProposedMove("S1", "m1", "R2")]
        )
        assert report.affected.teachers == 1
        assert report.affected.rooms == 1
        assert report.affected.sessions == 1

    def test_moving_nothing_affects_nobody(self) -> None:
        instance = _instance()
        report = analyse_impact(instance, _placement(), [])
        assert report.affected.is_empty
        assert report.is_neutral


class TestStabilityDelta:
    def test_moving_away_from_the_published_position_costs_stability(self) -> None:
        instance = _instance(
            base_assignments=(
                BaseAssignment("S1", "m0", "R1"),
                BaseAssignment("S2", "m2", "R2"),
            )
        )
        report = analyse_impact(instance, _placement(), [ProposedMove("S1", "m1")])
        assert report.stability_delta == 1

    def test_moving_back_to_the_published_position_recovers_it(self) -> None:
        instance = _instance(
            base_assignments=(
                BaseAssignment("S1", "m0", "R1"),
                BaseAssignment("S2", "m2", "R2"),
            )
        )
        displaced = _placement(s1="m3")
        report = analyse_impact(instance, displaced, [ProposedMove("S1", "m0")])
        assert report.stability_delta == -1

    def test_a_greenfield_schedule_has_no_stability_to_lose(self) -> None:
        instance = _instance()  # no base assignments
        report = analyse_impact(instance, _placement(), [ProposedMove("S1", "m1")])
        assert report.stability_delta == 0


class TestAffectedProgrammesAreMeasured:
    """SESSION 23: the count that was always zero.

    `_affected_counts` looked programmes up in `snapshot.offerings` through
    `getattr(..., ())` — a field `BaseSnapshot` has never had. The fallback
    made the loop a no-op, so **`programs` and `faculties` were always 0** no
    matter what a change touched.

    That is not a cosmetic count. `assess_scope` routes an approval on it:
    with 0, `programs > 1` never fires, so a cross-programme or cross-faculty
    change was assessed as the narrowest scope and sent to the shortest chain.
    CLAUDE.md puts exactly those two among the decisions that need a higher
    authority.

    It survived because every test of scope assessment builds
    `AffectedCounts` by hand, so nothing ever exercised the measurement.
    """

    def _instance_with_programmes(self):
        import dataclasses

        from academic_ai.domain.scheduling.fixtures import FIXTURES

        instance = FIXTURES["TRIVIAL_FEASIBLE"].instance()
        snapshot = instance.snapshot
        sessions = tuple(
            dataclasses.replace(
                spec, program_ids=("prog-a", "prog-b"), faculty_ids=("fac-1",)
            )
            for spec in snapshot.sessions
        )
        return dataclasses.replace(
            instance, snapshot=dataclasses.replace(snapshot, sessions=sessions)
        )

    def _move_one(self, instance):
        from academic_ai.domain.scheduling.impact import ProposedMove, analyse_impact
        from academic_ai.domain.scheduling.neighborhood import PlacedSession

        snapshot = instance.snapshot
        placement = {
            s.id: PlacedSession(
                session_id=s.id,
                start_slot_id=snapshot.slots[0].id,
                room_id=snapshot.rooms[0].id if snapshot.rooms else None,
            )
            for s in snapshot.sessions
        }
        target = snapshot.sessions[0]
        return analyse_impact(
            instance,
            placement,
            [
                ProposedMove(
                    session_id=target.id,
                    to_start_slot_id=snapshot.slots[1].id,
                    to_room_id=None,
                )
            ],
        )

    def test_moving_a_session_counts_its_programmes(self) -> None:
        report = self._move_one(self._instance_with_programmes())
        assert report.affected.programs == 2, (
            "affected programmes is still 0 — the change's reach is not measured"
        )
        assert report.affected.faculties == 1

    def test_a_session_serving_no_programme_counts_none(self) -> None:
        # Not the same statement as "we did not look". A session whose
        # offering serves no programme is a data problem worth seeing as zero.
        from academic_ai.domain.scheduling.fixtures import FIXTURES

        report = self._move_one(FIXTURES["TRIVIAL_FEASIBLE"].instance())
        assert report.affected.programs == 0

    def test_the_scope_assessment_now_sees_a_cross_programme_change(self) -> None:
        # The consequence, end to end: this is the assessment that chooses the
        # approval chain, and it could not previously reach anything but the
        # narrowest branch from a real impact report.
        from academic_ai.domain.enums import ScheduleScope
        from academic_ai.domain.governance.scope import assess_scope

        report = self._move_one(self._instance_with_programmes())
        assert assess_scope(report).scope is not ScheduleScope.PROGRAM
