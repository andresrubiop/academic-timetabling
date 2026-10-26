"""Feasibility analysis and recommendations (SESSION 6).

The playbook's rule for this session is one sentence: **never return only
"No solution."** Everything here defends that. A bare INFEASIBLE is true and
useless — it names nothing, suggests nothing, and reads exactly like a
timeout.

Also covers the three review findings SESSION 6 absorbed, because they are
the same work: the validator's coverage gate over the HARD catalog, the four
codes it never checked, and the MANDATORY-incidence check that names the
students a legal schedule stranded anyway.
"""

from __future__ import annotations

import pytest

from academic_ai.domain.enums import DayOfWeek, RecommendationKind
from academic_ai.domain.scheduling.catalog import solver_hard_codes
from academic_ai.domain.scheduling.feasibility import (
    Finding,
    InfeasibilityReport,
    Severity,
    analyse,
)
from academic_ai.domain.scheduling.fixtures import get_fixture
from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
from academic_ai.domain.scheduling.problem import (
    BaseSnapshot,
    CoTeachingGroupSpec,
    PeriodSpec,
    ProblemInstance,
    RoomSpec,
    SessionSpec,
    SlotSpec,
    StudentEnrollmentSpec,
    TeacherSpec,
)
from academic_ai.domain.scheduling.recommendations import (
    ImpactEvidence,
    Recommendation,
    RecommendationProvider,
    RecommendationRequest,
)
from academic_ai.optimization.validator import (
    Assignment,
    unchecked_hard_codes,
    validate,
)


def _instance(**kw) -> ProblemInstance:
    base = dict(
        period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,)),
        slots=tuple(
            SlotSpec(f"m{i}", DayOfWeek.MONDAY, i, i, 480 + 60 * i) for i in range(3)
        ),
        rooms=(RoomSpec("R1", "C1", 40, "CLASSROOM", has_declared_availability=True),),
        teachers=(TeacherSpec("T1", has_declared_availability=True),),
        sessions=(
            SessionSpec("S1", "sec", "off", "crs", "C1", 1, teacher_ids=("T1",)),
        ),
    )
    base.update(kw)
    return ProblemInstance(
        snapshot=BaseSnapshot(**base),  # type: ignore[arg-type]
        constraints=(),
        objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
    )


class TestNeverJustNoSolution:
    def test_an_error_must_name_its_subject(self) -> None:
        """An unnamed impossibility is no better than a bare INFEASIBLE."""
        with pytest.raises(ValueError, match="must name its subject"):
            Finding(Severity.ERROR, "X", "something is impossible")

    def test_a_warning_need_not_name_one(self) -> None:
        Finding(Severity.WARNING, "X", "something is odd")

    def test_the_summary_is_always_actionable(self) -> None:
        report = analyse(
            _instance(
                rooms=(RoomSpec("R1", "C1", 5, "CLASSROOM", has_declared_availability=True),),
                sessions=(
                    SessionSpec(
                        "S1", "sec", "off", "crs", "C1", 1,
                        teacher_ids=("T1",), seat_demand=500,
                    ),
                ),
            )
        )
        assert not report.is_solvable
        assert "S1" in report.summary()
        assert "No solution" not in report.summary()

    def test_a_clean_instance_says_so(self) -> None:
        report = analyse(_instance())
        assert report.is_solvable
        assert not report.errors


class TestStructuralImpossibilities:
    def test_a_session_with_no_usable_room(self) -> None:
        report = analyse(
            _instance(
                rooms=(RoomSpec("R1", "C1", 5, "CLASSROOM", has_declared_availability=True),),
                sessions=(
                    SessionSpec(
                        "S1", "sec", "off", "crs", "C1", 1,
                        teacher_ids=("T1",), seat_demand=500,
                    ),
                ),
            )
        )
        error = next(f for f in report.errors if f.code == "SESSION_HAS_NO_ROOM")
        assert error.subject_id == "S1"
        assert RecommendationKind.CHANGE_ROOM in error.remedies

    def test_a_session_with_no_usable_time(self) -> None:
        report = analyse(
            _instance(
                teachers=(
                    TeacherSpec(
                        "T1",
                        unavailable_slot_ids=frozenset({"m0", "m1", "m2"}),
                        has_declared_availability=True,
                    ),
                ),
            )
        )
        error = next(f for f in report.errors if f.code == "SESSION_HAS_NO_TIME")
        assert error.subject_id == "S1"

    def test_time_and_space_problems_are_told_apart(self) -> None:
        """Conflating them sends the coordinator to the wrong place."""
        no_time = analyse(
            _instance(
                teachers=(
                    TeacherSpec(
                        "T1", unavailable_slot_ids=frozenset({"m0", "m1", "m2"}),
                        has_declared_availability=True,
                    ),
                ),
            )
        )
        codes = {f.code for f in no_time.errors}
        assert "SESSION_HAS_NO_TIME" in codes
        assert "SESSION_HAS_NO_ROOM" not in codes, (
            "a time problem must not be reported as a space problem"
        )
        # Reporting the teacher's exhausted availability *as well* is correct:
        # both facts are true and the coordinator needs both.
        assert "TEACHER_HOURS_EXCEED_AVAILABILITY" in codes

    def test_a_teacher_asked_for_more_hours_than_they_have(self) -> None:
        report = analyse(
            _instance(
                sessions=tuple(
                    SessionSpec(
                        f"S{i}", f"sec{i}", f"off{i}", f"crs{i}", "C1", 1,
                        teacher_ids=("T1",),
                    )
                    for i in range(5)
                ),
            )
        )
        error = next(
            f for f in report.errors if f.code == "TEACHER_HOURS_EXCEED_AVAILABILITY"
        )
        assert error.subject_id == "T1"
        assert error.evidence["required_slots"] == 5
        assert error.evidence["available_slots"] == 3

    def test_a_contractual_weekly_limit_is_respected(self) -> None:
        report = analyse(
            _instance(
                teachers=(
                    TeacherSpec(
                        "T1", max_weekly_minutes=60, has_declared_availability=True
                    ),
                ),
                sessions=tuple(
                    SessionSpec(
                        f"S{i}", f"sec{i}", f"off{i}", f"crs{i}", "C1", 1,
                        teacher_ids=("T1",),
                    )
                    for i in range(3)
                ),
            )
        )
        assert any(
            f.code == "TEACHER_WEEKLY_LOAD_EXCEEDED" for f in report.errors
        )

    def test_more_room_slots_are_needed_than_exist(self) -> None:
        """A counting argument, so it holds however clever the search is."""
        report = analyse(
            _instance(
                sessions=tuple(
                    SessionSpec(
                        f"S{i}", f"sec{i}", f"off{i}", f"crs{i}", "C1", 1,
                        teacher_ids=(f"T{i}",),
                    )
                    for i in range(10)
                ),
                teachers=tuple(
                    TeacherSpec(f"T{i}", has_declared_availability=True)
                    for i in range(10)
                ),
            )
        )
        error = next(f for f in report.errors if f.code == "INSUFFICIENT_ROOM_SLOTS")
        assert error.evidence["demand"] == 10
        assert error.evidence["supply"] == 3

    def test_a_student_enrolled_in_more_than_the_week_holds(self) -> None:
        report = analyse(
            _instance(
                sessions=tuple(
                    SessionSpec(
                        f"S{i}", f"sec{i}", f"off{i}", f"crs{i}", "C1", 1,
                        teacher_ids=(f"T{i}",),
                    )
                    for i in range(4)
                ),
                teachers=tuple(
                    TeacherSpec(f"T{i}", has_declared_availability=True)
                    for i in range(4)
                ),
                rooms=tuple(
                    RoomSpec(f"R{i}", "C1", 40, "CLASSROOM", has_declared_availability=True)
                    for i in range(4)
                ),
                enrollments=tuple(
                    StudentEnrollmentSpec("E1", f"off{i}", (f"S{i}",), True)
                    for i in range(4)
                ),
            )
        )
        error = next(
            f for f in report.errors if f.code == "STUDENT_LOAD_EXCEEDS_WEEK"
        )
        assert error.subject_id == "E1"

    def test_an_unapproved_merge_is_an_error(self) -> None:
        instance = get_fixture("CO_TEACHING_UNAPPROVED").instance()
        report = analyse(instance)
        assert any(
            f.code == "CO_TEACHING_GROUP_NOT_APPROVED" for f in report.errors
        )

    def test_all_errors_are_reported_at_once(self) -> None:
        """A coordinator handed one impossibility at a time is being made to
        play twenty questions with their own timetable."""
        report = analyse(
            _instance(
                rooms=(RoomSpec("R1", "C1", 1, "CLASSROOM", has_declared_availability=True),),
                sessions=tuple(
                    SessionSpec(
                        f"S{i}", f"sec{i}", f"off{i}", f"crs{i}", "C1", 1,
                        teacher_ids=("T1",), seat_demand=99,
                    )
                    for i in range(5)
                ),
            )
        )
        assert len({f.code for f in report.errors}) >= 2


class TestWarningsAreTheDangerousCategory:
    def test_undeclared_availability_is_a_warning_not_an_error(self) -> None:
        """Solvable, and quietly wrong: the schedule looks fine and the
        teacher rejects it."""
        report = analyse(
            _instance(teachers=(TeacherSpec("T1"),))
        )
        assert report.is_solvable
        assert any(
            f.code == "TEACHER_AVAILABILITY_UNDECLARED" for f in report.warnings
        )

    def test_an_unstaffed_session_is_a_warning(self) -> None:
        report = analyse(
            _instance(
                sessions=(SessionSpec("S1", "sec", "off", "crs", "C1", 1),),
            )
        )
        assert any(f.code == "SESSION_HAS_NO_TEACHER" for f in report.warnings)

    def test_unresolvable_conflict_risk_is_surfaced_before_the_search(self) -> None:
        """The population the platform exists to protect, named before a
        search rather than discovered after one."""
        report = analyse(
            _instance(
                sessions=(
                    SessionSpec("S1", "s1", "A", "c1", "C1", 1, teacher_ids=("T1",)),
                    SessionSpec("S2", "s2", "B", "c2", "C1", 1, teacher_ids=("T1",)),
                ),
                enrollments=(
                    StudentEnrollmentSpec("E1", "A", ("S1",), True),
                    StudentEnrollmentSpec("E1", "B", ("S2",), True),
                ),
            )
        )
        warning = next(
            f for f in report.warnings if f.code == "UNRESOLVABLE_CONFLICT_RISK"
        )
        assert warning.evidence["student_count"] == 1


class TestSuggestions:
    def test_a_tight_room_supply_is_worth_knowing(self) -> None:
        report = analyse(
            _instance(
                sessions=tuple(
                    SessionSpec(
                        f"S{i}", f"sec{i}", f"off{i}", f"crs{i}", "C1", 1,
                        teacher_ids=(f"T{i}",),
                    )
                    for i in range(3)
                ),
                teachers=tuple(
                    TeacherSpec(f"T{i}", has_declared_availability=True)
                    for i in range(3)
                ),
            )
        )
        assert report.is_solvable
        assert any(f.code == "ROOM_SUPPLY_TIGHT" for f in report.suggestions)


class TestRecommendationContract:
    def test_a_recommendation_must_say_why(self) -> None:
        with pytest.raises(ValueError, match="must say why"):
            Recommendation(
                RecommendationKind.MOVE_SESSION, "class_session", "S1", "  ",
                ImpactEvidence(),
            )

    def test_impact_evidence_is_required(self) -> None:
        """A recommendation without evidence is somebody's guess with an
        official-looking border around it."""
        import inspect

        signature = inspect.signature(Recommendation)
        assert signature.parameters["impact"].default is inspect.Parameter.empty

    def test_improvement_is_judged_lexicographically(self) -> None:
        better = ImpactEvidence(score_delta=(0, -5, 100), tier_codes=("A", "B", "C"))
        worse = ImpactEvidence(score_delta=(1, -50, -100), tier_codes=("A", "B", "C"))
        assert better.is_improvement
        assert not worse.is_improvement

    def test_a_neutral_change_is_not_an_improvement(self) -> None:
        neutral = ImpactEvidence(score_delta=(0, 0), tier_codes=("A", "B"))
        assert neutral.is_neutral
        assert not neutral.is_improvement

    def test_confidence_must_be_a_probability(self) -> None:
        with pytest.raises(ValueError, match="confidence"):
            Recommendation(
                RecommendationKind.MOVE_SESSION, "class_session", "S1", "because",
                ImpactEvidence(), confidence=1.5,
            )

    def test_a_heuristic_provider_satisfies_the_protocol(self) -> None:
        class Simple:
            code = "SIMPLE"

            def recommend(self, request: RecommendationRequest):
                return ()

        assert isinstance(Simple(), RecommendationProvider)


class TestValidatorCoverageGate:
    def test_the_validator_checks_every_hard_code(self) -> None:
        """The same safety property `missing_adapters()` gives the builder.

        A HARD constraint checked by the builder but not here has no
        independent confirmation at all — and the builder is exactly what the
        validator exists to disbelieve. Four codes were in this state,
        including `ROOM_TYPE_ALLOWED`, which had also been unenforced in the
        builder: the second line of defence was absent for precisely the bug
        it exists to catch.
        """
        assert unchecked_hard_codes() == frozenset()

    def test_checked_reports_what_was_actually_checked(self) -> None:
        instance = get_fixture("TRIVIAL_FEASIBLE").instance()
        report = validate(instance, (Assignment("S1", "mon0", "R1"),))
        assert "ROOM_NO_OVERLAP" in report.checked
        assert "TEACHER_NO_OVERLAP" in report.checked

    def test_the_validator_catches_a_forged_room_type_violation(self) -> None:
        instance = ProblemInstance(
            snapshot=BaseSnapshot(
                period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,)),
                slots=(SlotSpec("m0", DayOfWeek.MONDAY, 0, 0, 480),),
                rooms=(
                    RoomSpec("OK", "C1", 40, "CLASSROOM", has_declared_availability=True),
                    RoomSpec("NO", "C1", 40, "CLASSROOM", has_declared_availability=True),
                ),
                teachers=(TeacherSpec("T1", has_declared_availability=True),),
                sessions=(
                    SessionSpec(
                        "S1", "sec", "off", "crs", "C1", 1,
                        teacher_ids=("T1",),
                        allowed_room_ids=frozenset({"OK"}),
                    ),
                ),
            ),
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        report = validate(instance, (Assignment("S1", "m0", "NO"),))
        assert "ROOM_TYPE_ALLOWED" in report.by_code()


class TestStrandedStudentsAreNamed:
    def test_a_mandatory_clash_with_no_free_alternative_is_reported(self) -> None:
        """The check OPTIMIZER.md §6.6 required and nothing implemented.

        `has_alternative` is static and optimistic: it asks whether an
        alternative exists, not whether it is free in the schedule produced.
        The recorded price was that the validator would name the students it
        stranded anyway.
        """
        instance = ProblemInstance(
            snapshot=BaseSnapshot(
                period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,)),
                slots=(SlotSpec("m0", DayOfWeek.MONDAY, 0, 0, 480),),
                sessions=(
                    SessionSpec("S1", "s1", "A", "c1", "C1", 1, requires_room=False),
                    SessionSpec("S2", "s2", "B", "c2", "C1", 1, requires_room=False),
                ),
                enrollments=(
                    StudentEnrollmentSpec(
                        "E1", "A", ("S1",), True, alternative_section_count=1
                    ),
                    StudentEnrollmentSpec(
                        "E1", "B", ("S2",), True, alternative_section_count=1
                    ),
                ),
            ),
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        report = validate(
            instance, (Assignment("S1", "m0"), Assignment("S2", "m0"))
        )
        assert report.is_valid, "the schedule is legal — that is the point"
        assert report.has_stranded_students
        assert report.stranded_students() == ("E1",)
        assert report.unresolved_incidences[0].criticality > 0

    def test_a_schedule_with_no_clash_strands_nobody(self) -> None:
        instance = ProblemInstance(
            snapshot=BaseSnapshot(
                period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,)),
                slots=(
                    SlotSpec("m0", DayOfWeek.MONDAY, 0, 0, 480),
                    SlotSpec("m1", DayOfWeek.MONDAY, 1, 1, 540),
                ),
                sessions=(
                    SessionSpec("S1", "s1", "A", "c1", "C1", 1, requires_room=False),
                    SessionSpec("S2", "s2", "B", "c2", "C1", 1, requires_room=False),
                ),
                enrollments=(
                    StudentEnrollmentSpec(
                        "E1", "A", ("S1",), True, alternative_section_count=1
                    ),
                    StudentEnrollmentSpec(
                        "E1", "B", ("S2",), True, alternative_section_count=1
                    ),
                ),
            ),
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        report = validate(
            instance, (Assignment("S1", "m0"), Assignment("S2", "m1"))
        )
        assert not report.has_stranded_students
