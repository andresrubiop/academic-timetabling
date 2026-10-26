"""Student Conflict Graph, criticality, and feasibility (SESSION 5).

The playbook's headline rule is that *one student with a repeated mandatory
course and only one section may be more critical than many students with
multiple alternatives*. The most important test here is the one that shows
**where that rule actually lives**: in the lexicographic category, not in a
weight. One unresolvable student outranks any number of resolvable ones by
construction, and no amount of criticality can trade them off.

Criticality then ranks *within* a category, which is a genuine question with
no categorical answer.

The seven scenarios the session names each get a test:
normal cohort, repeated course, single-section repeated course, multiple
alternatives, shared cross-program course, elective, advanced course.
"""

from __future__ import annotations

import pytest

from academic_ai.domain.enums import DayOfWeek
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
from academic_ai.domain.scheduling.problem import (
    BaseSnapshot,
    PeriodSpec,
    ProblemInstance,
    SessionSpec,
    SlotSpec,
    StudentEnrollmentSpec,
    classify_student_conflict,
)
from academic_ai.domain.scheduling.students import (
    CRITICALITY_SCALE,
    FeasibilityScope,
    build_conflict_graph,
    criticality,
    feasibility,
    student_feasibility,
)


def _enrollment(**kw) -> StudentEnrollmentSpec:
    base = dict(
        student_id="E1",
        offering_id="O1",
        session_ids=("S1",),
        is_mandatory=True,
    )
    base.update(kw)
    return StudentEnrollmentSpec(**base)  # type: ignore[arg-type]


def _instance(enrollments, sessions=("S1", "S2")) -> ProblemInstance:
    return ProblemInstance(
        snapshot=BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,)),
            slots=(
                SlotSpec("m0", DayOfWeek.MONDAY, 0, 0, 480),
                SlotSpec("m1", DayOfWeek.MONDAY, 1, 1, 540),
            ),
            sessions=tuple(
                SessionSpec(
                    id=sid,
                    section_id=f"SEC-{sid}",
                    offering_id=f"OFF-{sid}",
                    course_id=f"CRS-{sid}",
                    campus_id="C1",
                    duration_slots=1,
                    requires_room=False,
                )
                for sid in sessions
            ),
            enrollments=tuple(enrollments),
        ),
        constraints=(),
        objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
    )


def _clashing(sessions=("S1", "S2")) -> dict[str, PlacedSession]:
    """Everything in the same slot."""
    return {sid: PlacedSession(sid, "m0") for sid in sessions}


def _separated() -> dict[str, PlacedSession]:
    return {"S1": PlacedSession("S1", "m0"), "S2": PlacedSession("S2", "m1")}


class TestWhereTheCriticalRuleLives:
    def test_one_stuck_student_outranks_many_flexible_ones_by_category(self) -> None:
        """The headline rule, and the point of the whole design.

        The stuck student's clash is HARD; the twenty flexible students' are
        MEDIUM, three tiers lower. No weight can trade them off, which is
        exactly right: "a student loses their year" and "twenty students are
        inconvenienced" have no honest exchange rate.
        """
        stuck_a = _enrollment(student_id="STUCK", offering_id="A", session_ids=("S1",))
        stuck_b = _enrollment(student_id="STUCK", offering_id="B", session_ids=("S2",))
        assert (
            classify_student_conflict(stuck_a, stuck_b)
            == "STUDENT_NO_OVERLAP_UNRESOLVABLE"
        )

        flexible_a = _enrollment(
            student_id="FLEX", offering_id="A", session_ids=("S1",),
            alternative_section_count=3,
        )
        flexible_b = _enrollment(
            student_id="FLEX", offering_id="B", session_ids=("S2",),
            alternative_section_count=3,
        )
        assert (
            classify_student_conflict(flexible_a, flexible_b)
            == "STUDENT_NO_OVERLAP_MANDATORY"
        )

    def test_twenty_flexible_students_do_not_outweigh_one_stuck_one(self) -> None:
        """Feasibility is decided by the stranded, not by the inconvenienced."""
        enrollments = [
            _enrollment(student_id="STUCK", offering_id="A", session_ids=("S1",)),
            _enrollment(student_id="STUCK", offering_id="B", session_ids=("S2",)),
        ]
        for i in range(20):
            enrollments += [
                _enrollment(
                    student_id=f"F{i}", offering_id="A", session_ids=("S1",),
                    alternative_section_count=3,
                ),
                _enrollment(
                    student_id=f"F{i}", offering_id="B", session_ids=("S2",),
                    alternative_section_count=3,
                ),
            ]
        instance = _instance(enrollments)
        report = feasibility(instance, _clashing())[0]

        assert report.affected_students == 21
        assert report.unresolvable_students == 1
        assert not report.is_feasible, (
            "one stranded student makes the scope infeasible however many "
            "others merely need re-sectioning"
        )
        assert report.mandatory_incidences == 20


class TestCriticalityIsAdditive:
    def test_a_pair_weight_is_the_sum_of_its_students(self) -> None:
        """The contract the CP-SAT encoding depends on.

        Conflicts are aggregated by distinct session pair with an integer
        multiplicity — one literal per pair, not per (student, pair). That is
        only valid if the pair's weight is the sum of its students'.
        """
        enrollments = []
        for i in range(3):
            enrollments += [
                _enrollment(student_id=f"E{i}", offering_id="A", session_ids=("S1",)),
                _enrollment(student_id=f"E{i}", offering_id="B", session_ids=("S2",)),
            ]
        graph = build_conflict_graph(_instance(enrollments))
        edge = graph.edges[0]

        expected = sum(
            criticality(e) for e in enrollments if e.student_id in edge.student_ids
        )
        assert edge.weight == expected
        assert edge.affected_students == 3

    def test_criticality_reads_one_enrollment_only(self) -> None:
        """A function of one enrollment cannot be non-additive."""
        alone = criticality(_enrollment())
        assert alone == criticality(_enrollment(student_id="SOMEBODY_ELSE"))


class TestCriticalityRanking:
    def test_fewer_options_means_higher_criticality(self) -> None:
        stuck = criticality(_enrollment())
        one_way_out = criticality(_enrollment(alternative_section_count=1))
        three_ways_out = criticality(_enrollment(alternative_section_count=3))
        assert stuck > one_way_out > three_ways_out

    def test_an_alternative_offering_counts_as_much_as_a_section(self) -> None:
        """ADR-0009 lists both as resolution routes."""
        assert criticality(_enrollment(alternative_section_count=1)) == criticality(
            _enrollment(alternative_offering_count=1)
        )

    def test_mandatory_outranks_elective(self) -> None:
        """An elective can be dropped; that is a move the student keeps."""
        assert criticality(_enrollment()) > criticality(
            _enrollment(is_mandatory=False)
        )

    def test_each_repeat_attempt_raises_it(self) -> None:
        first = criticality(_enrollment())
        second = criticality(_enrollment(is_repeat=True, attempt_number=2))
        third = criticality(_enrollment(is_repeat=True, attempt_number=3))
        assert third > second > first

    def test_the_repeat_multiplier_is_capped(self) -> None:
        """An uncapped multiplier would let one extreme record dominate a
        whole faculty."""
        tenth = criticality(_enrollment(is_repeat=True, attempt_number=10))
        fourth = criticality(_enrollment(is_repeat=True, attempt_number=4))
        assert tenth == fourth

    def test_it_is_an_integer_so_it_can_enter_a_cp_sat_objective(self) -> None:
        value = criticality(_enrollment())
        assert isinstance(value, int)
        assert value == 2 * CRITICALITY_SCALE  # mandatory, no alternatives


class TestConflictGraph:
    def test_an_edge_exists_only_where_a_student_is_shared(self) -> None:
        graph = build_conflict_graph(
            _instance(
                [
                    _enrollment(student_id="E1", offering_id="A", session_ids=("S1",)),
                    _enrollment(student_id="E1", offering_id="B", session_ids=("S2",)),
                ]
            )
        )
        assert [e.key for e in graph.edges] == [("S1", "S2")]

    def test_students_with_no_shared_session_create_no_edge(self) -> None:
        graph = build_conflict_graph(
            _instance(
                [
                    _enrollment(student_id="E1", offering_id="A", session_ids=("S1",)),
                    _enrollment(student_id="E2", offering_id="B", session_ids=("S2",)),
                ]
            )
        )
        assert graph.edges == ()

    def test_the_graph_is_independent_of_any_schedule(self) -> None:
        """It says what *would* clash, which is what makes it usable for
        choosing a neighbourhood before anything is placed."""
        instance = _instance(
            [
                _enrollment(student_id="E1", offering_id="A", session_ids=("S1",)),
                _enrollment(student_id="E1", offering_id="B", session_ids=("S2",)),
            ]
        )
        graph = build_conflict_graph(instance)
        assert len(graph.edges) == 1
        assert len(graph.realised(instance, _clashing())) == 1
        assert len(graph.realised(instance, _separated())) == 0

    def test_one_unresolvable_student_makes_the_whole_edge_unresolvable(self) -> None:
        """However many others on that edge could be re-sectioned."""
        enrollments = [
            _enrollment(student_id="STUCK", offering_id="A", session_ids=("S1",)),
            _enrollment(student_id="STUCK", offering_id="B", session_ids=("S2",)),
            _enrollment(
                student_id="FLEX", offering_id="A", session_ids=("S1",),
                alternative_section_count=5,
            ),
            _enrollment(
                student_id="FLEX", offering_id="B", session_ids=("S2",),
                alternative_section_count=5,
            ),
        ]
        graph = build_conflict_graph(_instance(enrollments))
        assert graph.edges[0].is_unresolvable

    def test_components_group_sessions_that_can_resolve_each_other(self) -> None:
        instance = _instance(
            [
                _enrollment(student_id="E1", offering_id="A", session_ids=("S1",)),
                _enrollment(student_id="E1", offering_id="B", session_ids=("S2",)),
            ],
            sessions=("S1", "S2", "S3"),
        )
        components = build_conflict_graph(instance).components()
        assert {frozenset({"S1", "S2"}), frozenset({"S3"})} == set(components)

    def test_pressure_sums_the_criticality_riding_on_a_session(self) -> None:
        graph = build_conflict_graph(
            _instance(
                [
                    _enrollment(student_id="E1", offering_id="A", session_ids=("S1",)),
                    _enrollment(student_id="E1", offering_id="B", session_ids=("S2",)),
                ]
            )
        )
        assert graph.pressure("S1") == graph.edges[0].weight
        assert graph.degree("S1") == 1


class TestTheSevenScenarios:
    """The cases the playbook names for SESSION 5."""

    def _two_courses(self, **kw) -> ProblemInstance:
        a = _enrollment(student_id="E1", offering_id="A", session_ids=("S1",), **kw)
        b = _enrollment(student_id="E1", offering_id="B", session_ids=("S2",), **kw)
        return _instance([a, b])

    def test_normal_cohort(self) -> None:
        """Mandatory courses with alternatives: resolvable, so feasible."""
        instance = self._two_courses(alternative_section_count=2)
        report = feasibility(instance, _clashing())[0]
        assert report.is_feasible
        assert report.mandatory_incidences == 1

    def test_repeated_course_with_alternatives(self) -> None:
        """A repeat is not by itself fatal — four sections means three moves."""
        instance = self._two_courses(
            is_repeat=True, attempt_number=2, alternative_section_count=3
        )
        report = feasibility(instance, _clashing())[0]
        assert report.is_feasible
        assert report.unresolvable_incidences == 0

    def test_single_section_repeated_course(self) -> None:
        """The case the platform exists to protect."""
        instance = self._two_courses(is_repeat=True, attempt_number=2)
        report = feasibility(instance, _clashing())[0]
        assert not report.is_feasible
        assert report.unresolvable_students == 1

    def test_multiple_alternatives(self) -> None:
        instance = self._two_courses(
            alternative_section_count=3, alternative_offering_count=2
        )
        report = feasibility(instance, _clashing())[0]
        assert report.is_feasible

    def test_shared_cross_program_course(self) -> None:
        """Two programmes' students in one session still conflict per student."""
        enrollments = [
            _enrollment(student_id="SIS", offering_id="SHARED", session_ids=("S1",)),
            _enrollment(student_id="SIS", offering_id="B", session_ids=("S2",)),
            _enrollment(student_id="IND", offering_id="SHARED", session_ids=("S1",)),
            _enrollment(student_id="IND", offering_id="B", session_ids=("S2",)),
        ]
        graph = build_conflict_graph(_instance(enrollments))
        assert graph.edges[0].affected_students == 2

    def test_elective(self) -> None:
        """The student keeps the option of dropping it."""
        a = _enrollment(student_id="E1", offering_id="A", session_ids=("S1",))
        b = _enrollment(
            student_id="E1", offering_id="B", session_ids=("S2",), is_mandatory=False
        )
        report = feasibility(_instance([a, b]), _clashing())[0]
        assert report.is_feasible
        assert report.elective_incidences == 1

    def test_advanced_course(self) -> None:
        """Taken ahead of schedule, so no progression depends on it now."""
        a = _enrollment(student_id="E1", offering_id="A", session_ids=("S1",))
        b = _enrollment(
            student_id="E1",
            offering_id="B",
            session_ids=("S2",),
            is_mandatory=False,
            alternative_offering_count=1,
        )
        report = feasibility(_instance([a, b]), _clashing())[0]
        assert report.is_feasible


class TestFeasibilityScopes:
    def test_a_clash_free_schedule_is_feasible(self) -> None:
        instance = _instance(
            [
                _enrollment(student_id="E1", offering_id="A", session_ids=("S1",)),
                _enrollment(student_id="E1", offering_id="B", session_ids=("S2",)),
            ]
        )
        report = feasibility(instance, _separated())[0]
        assert report.is_feasible
        assert report.affected_students == 0

    def test_student_scope(self) -> None:
        instance = _instance(
            [
                _enrollment(student_id="E1", offering_id="A", session_ids=("S1",)),
                _enrollment(student_id="E1", offering_id="B", session_ids=("S2",)),
            ]
        )
        report = student_feasibility(instance, _clashing(), "E1")
        assert report.scope is FeasibilityScope.STUDENT
        assert report.students == 1
        assert not report.is_feasible

    def test_program_scope_partitions_students(self) -> None:
        enrollments = []
        for student, ok in (("SIS1", False), ("IND1", True)):
            enrollments += [
                _enrollment(
                    student_id=student, offering_id="A", session_ids=("S1",),
                    alternative_section_count=3 if ok else 0,
                ),
                _enrollment(
                    student_id=student, offering_id="B", session_ids=("S2",),
                    alternative_section_count=3 if ok else 0,
                ),
            ]
        reports = feasibility(
            _instance(enrollments),
            _clashing(),
            FeasibilityScope.PROGRAM,
            students={"SIS1": "SIS", "IND1": "IND"},
        )
        by_id = {r.scope_id: r for r in reports}
        assert not by_id["SIS"].is_feasible
        assert by_id["IND"].is_feasible

    def test_institution_scope_is_the_default(self) -> None:
        instance = _instance([_enrollment()])
        reports = feasibility(instance, _clashing())
        assert len(reports) == 1
        assert reports[0].scope is FeasibilityScope.INSTITUTION

    def test_affected_fraction_is_reported(self) -> None:
        enrollments = []
        for i in range(4):
            enrollments += [
                _enrollment(student_id=f"E{i}", offering_id="A", session_ids=("S1",)),
                _enrollment(student_id=f"E{i}", offering_id="B", session_ids=("S2",)),
            ]
        report = feasibility(_instance(enrollments), _clashing())[0]
        assert report.affected_fraction == 1.0


class TestNotEveryConflictIsHard:
    def test_the_session_does_not_promote_all_conflicts(self) -> None:
        """SESSION 5 says so in as many words: "Do not silently convert all
        student conflicts into HARD"."""
        resolvable = _instance(
            [
                _enrollment(
                    student_id="E1", offering_id="A", session_ids=("S1",),
                    alternative_section_count=2,
                ),
                _enrollment(
                    student_id="E1", offering_id="B", session_ids=("S2",),
                    alternative_section_count=2,
                ),
            ]
        )
        graph = build_conflict_graph(resolvable)
        assert not graph.edges[0].is_unresolvable
        assert feasibility(resolvable, _clashing())[0].is_feasible
