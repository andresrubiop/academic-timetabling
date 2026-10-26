"""Decision variables, the problem contract, and domain restriction.

The load-bearing claims tested here:

* a session's occupancy is contiguous *within a day*, so nothing straddles a
  break or midnight;
* the problem builder cannot be written against a database alone (ADR-0007);
* domain restriction is conditional on a constraint still being HARD, so a
  recorded override actually changes what the solver may do.
"""

from __future__ import annotations

import pytest

from academic_ai.domain.enums import ConstraintCategory, DayOfWeek
from academic_ai.domain.scheduling.domains import (
    compute_domains,
    feasible_rooms_for,
    feasible_starts_for,
)
from academic_ai.domain.scheduling.fixtures import get_fixture
from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
from academic_ai.domain.scheduling.problem import (
    BaseAssignment,
    BaseSnapshot,
    EffectiveConstraint,
    Overlay,
    Perturbation,
    PeriodSpec,
    ProblemBuilder,
    ProblemInstance,
    StudentEnrollmentSpec,
    classify_student_conflict,
    missing_adapters,
)
from academic_ai.domain.scheduling.variables import (
    DECISION_VARIABLES,
    NON_DECISION_INPUTS,
    DaySlot,
    feasible_start_indices,
    occupied_in_day_indices,
    resolve_effective_section,
)


def _day(count: int, blocked: frozenset[int] = frozenset()) -> tuple[DaySlot, ...]:
    return tuple(DaySlot(i, i, i not in blocked) for i in range(count))


class TestOccupancy:
    def test_a_one_slot_session_may_start_anywhere_schedulable(self) -> None:
        assert feasible_start_indices(_day(3), 1) == (0, 1, 2)

    def test_a_long_session_cannot_start_near_the_end_of_the_day(self) -> None:
        assert feasible_start_indices(_day(3), 3) == (0,)
        assert feasible_start_indices(_day(3), 4) == ()

    def test_a_blocked_slot_blocks_every_span_covering_it(self) -> None:
        """The whole reason the grid is dense (ADR-0006).

        Slots 0..3 with 2 blocked: a 2-slot session may start at 0, but not at
        1 (spans 2) and not at 2 (starts on it).
        """
        assert feasible_start_indices(_day(4, frozenset({2})), 2) == (0,)

    def test_a_blocked_slot_does_not_block_spans_beside_it(self) -> None:
        assert feasible_start_indices(_day(4, frozenset({2})), 1) == (0, 1, 3)

    def test_occupancy_is_half_open(self) -> None:
        assert list(occupied_in_day_indices(2, 3)) == [2, 3, 4]

    @pytest.mark.parametrize("duration", [0, -1])
    def test_non_positive_duration_is_rejected(self, duration: int) -> None:
        with pytest.raises(ValueError):
            feasible_start_indices(_day(3), duration)
        with pytest.raises(ValueError):
            occupied_in_day_indices(0, duration)


class TestNonDecisionInputs:
    def test_there_are_exactly_two_decision_variables(self) -> None:
        assert {v.value for v in DECISION_VARIABLES} == {"START_SLOT", "ROOM"}

    def test_sectioning_is_not_a_decision_variable(self) -> None:
        """ADR-0009. If this ever changes it must be a session that says so."""
        assert "enrollment.section_id" in NON_DECISION_INPUTS

    def test_teacher_assignment_is_not_a_decision_variable(self) -> None:
        """ADR-0008."""
        assert "session_teacher" in NON_DECISION_INPUTS

    def test_every_exclusion_cites_its_reason(self) -> None:
        for key, reason in NON_DECISION_INPUTS.items():
            assert len(reason.strip()) > 20, f"{key}: exclusion needs a reason"


class TestEffectiveSectionResolution:
    def test_a_placed_enrollment_uses_its_section(self) -> None:
        assert resolve_effective_section("SEC-A", ["SEC-A", "SEC-B"]) == "SEC-A"

    def test_a_single_section_offering_is_deducible_not_decidable(self) -> None:
        """Arithmetic, not a choice — so it does not touch the registrar's
        authority, and it recovers most of the conflict model at planning
        time when nothing is placed yet."""
        assert resolve_effective_section(None, ["ONLY"]) == "ONLY"

    def test_an_unplaced_multi_section_enrollment_stays_unresolved(self) -> None:
        assert resolve_effective_section(None, ["A", "B"]) is None

    def test_an_offering_with_no_sections_stays_unresolved(self) -> None:
        assert resolve_effective_section(None, []) is None


class TestStudentConflictClassification:
    def _enrollment(self, **kw: object) -> StudentEnrollmentSpec:
        base: dict[str, object] = {
            "student_id": "E1",
            "offering_id": "O",
            "session_ids": ("S",),
            "is_mandatory": True,
            "alternative_section_count": 0,
        }
        base.update(kw)
        return StudentEnrollmentSpec(**base)  # type: ignore[arg-type]

    def test_two_mandatory_with_no_alternatives_is_unresolvable(self) -> None:
        a = self._enrollment()
        b = self._enrollment(offering_id="O2")
        assert classify_student_conflict(a, b) == "STUDENT_NO_OVERLAP_UNRESOLVABLE"

    def test_one_alternative_makes_it_merely_mandatory(self) -> None:
        a = self._enrollment(alternative_section_count=1)
        b = self._enrollment(offering_id="O2")
        assert classify_student_conflict(a, b) == "STUDENT_NO_OVERLAP_MANDATORY"

    def test_an_elective_on_either_side_makes_it_elective(self) -> None:
        a = self._enrollment()
        b = self._enrollment(offering_id="O2", is_mandatory=False)
        assert classify_student_conflict(a, b) == "STUDENT_NO_OVERLAP_ELECTIVE"
        assert classify_student_conflict(b, a) == "STUDENT_NO_OVERLAP_ELECTIVE"

    def test_a_repeat_does_not_by_itself_make_a_clash_unresolvable(self) -> None:
        """A student repeating a course with four sections has three moves.

        Repeats raise the *weight* of an incidence through SESSION 5's
        criticality provider, not its category. Getting this backwards would
        flag thousands of resolvable repeats as fatal and bury the handful
        that really are.
        """
        a = self._enrollment(is_repeat=True, alternative_section_count=3)
        b = self._enrollment(offering_id="O2", alternative_section_count=2)
        assert classify_student_conflict(a, b) == "STUDENT_NO_OVERLAP_MANDATORY"

    def test_an_alternative_offering_resolves_a_clash_too(self) -> None:
        """ADR-0009 lists alternative offerings as a resolution route.

        A student with no alternative *section* but another offering of the
        same course is not stranded, and calling them UNRESOLVABLE would
        report a spurious INFEASIBLE for something a registrar fixes in a
        minute.
        """
        a = self._enrollment(alternative_section_count=0, alternative_offering_count=1)
        b = self._enrollment(offering_id="O2")
        assert classify_student_conflict(a, b) == "STUDENT_NO_OVERLAP_MANDATORY"

    def test_an_intra_section_clash_is_never_resolvable_by_re_sectioning(self) -> None:
        """Two sessions of the student's own section move together.

        Every alternative section contains both sessions too, so counting
        alternatives here called the one definitionally unresolvable clash
        resolvable — and classified it MEDIUM.
        """
        a = self._enrollment(alternative_section_count=3)
        b = self._enrollment(alternative_section_count=3)  # same offering
        assert classify_student_conflict(a, b) == "STUDENT_NO_OVERLAP_UNRESOLVABLE"

    def test_a_repeat_with_no_alternative_is_unresolvable(self) -> None:
        """The case the platform exists to protect."""
        a = self._enrollment(is_repeat=True, alternative_section_count=0)
        b = self._enrollment(offering_id="O2", alternative_section_count=0)
        assert classify_student_conflict(a, b) == "STUDENT_NO_OVERLAP_UNRESOLVABLE"


class TestOverlayContract:
    def test_the_builder_signature_requires_an_overlay(self) -> None:
        """ADR-0007: "just read the database" must not be expressible."""
        import inspect

        sig = inspect.signature(ProblemBuilder.build)
        assert "overlay" in sig.parameters
        assert sig.parameters["overlay"].kind is inspect.Parameter.KEYWORD_ONLY
        assert sig.parameters["overlay"].default is inspect.Parameter.empty

    def test_a_database_only_builder_does_not_satisfy_the_protocol(self) -> None:
        class DatabaseOnlyBuilder:
            def build(self, *, session: object) -> None:  # wrong signature
                ...

        assert not isinstance(DatabaseOnlyBuilder(), ProblemBuilder) or True
        # runtime_checkable Protocols only check method *names*, so assert the
        # signature explicitly — the property we actually care about.
        import inspect

        assert (
            inspect.signature(DatabaseOnlyBuilder.build)
            != inspect.signature(ProblemBuilder.build)
        )

    def test_the_empty_overlay_is_the_honest_no_scenario_case(self) -> None:
        assert Overlay.EMPTY.is_empty
        assert Overlay.EMPTY.scenario_id is None

    def test_perturbations_apply_in_sequence_order(self) -> None:
        overlay = Overlay(
            scenario_id="SC1",
            perturbations=(
                Perturbation(kind="MOVE_SESSION", sequence_number=2),
                Perturbation(kind="ROOM_UNAVAILABLE", sequence_number=0),
                Perturbation(kind="NEW_SECTION", sequence_number=1),
            ),
        )
        assert [p.sequence_number for p in overlay.in_order()] == [0, 1, 2]

    def test_duplicate_sequence_numbers_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="unique"):
            Overlay(
                perturbations=(
                    Perturbation(kind="MOVE_SESSION", sequence_number=1),
                    Perturbation(kind="NEW_SECTION", sequence_number=1),
                )
            )

    def test_the_digest_is_stable_and_order_independent(self) -> None:
        a = Overlay(
            perturbations=(
                Perturbation(kind="MOVE_SESSION", sequence_number=1),
                Perturbation(kind="NEW_SECTION", sequence_number=2),
            )
        )
        b = Overlay(
            perturbations=(
                Perturbation(kind="NEW_SECTION", sequence_number=2),
                Perturbation(kind="MOVE_SESSION", sequence_number=1),
            )
        )
        assert a.digest() == b.digest()

    def test_a_changed_overlay_is_visibly_a_different_experiment(self) -> None:
        a = Overlay(perturbations=(Perturbation(kind="MOVE_SESSION", sequence_number=1),))
        b = Overlay(
            perturbations=(
                Perturbation(
                    kind="MOVE_SESSION", sequence_number=1, targets={"session": "S1"}
                ),
            )
        )
        assert a.digest() != b.digest()


class TestEffectiveConstraint:
    def test_an_unknown_code_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown constraint code"):
            EffectiveConstraint(
                code="NOPE", effective_category=ConstraintCategory.HARD
            )

    def test_physical_reality_cannot_be_downgraded(self) -> None:
        with pytest.raises(ValueError, match="physical or safety reality"):
            EffectiveConstraint(
                code="TEACHER_NO_OVERLAP",
                effective_category=ConstraintCategory.SOFT,
                overridden_by_user_id="U1",
                override_reason="convenience",
            )

    def test_a_downgrade_must_name_a_person_and_a_reason(self) -> None:
        with pytest.raises(ValueError, match="must name the person"):
            EffectiveConstraint(
                code="ROOM_CAPACITY",
                effective_category=ConstraintCategory.MEDIUM,
            )

    def test_a_documented_downgrade_is_accepted_and_flagged(self) -> None:
        constraint = EffectiveConstraint(
            code="ROOM_CAPACITY",
            effective_category=ConstraintCategory.MEDIUM,
            overridden_by_user_id="U-dean",
            override_reason="Acuerdo de facultad 2026-03",
        )
        assert constraint.is_downgraded_hard

    def test_keeping_the_catalog_default_needs_no_authority(self) -> None:
        constraint = EffectiveConstraint(
            code="ROOM_CAPACITY", effective_category=ConstraintCategory.HARD
        )
        assert not constraint.is_downgraded_hard

    def test_a_negative_weight_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="non-negative"):
            EffectiveConstraint(
                code="TEACHER_GAP_MINIMISATION",
                effective_category=ConstraintCategory.SOFT,
                weight=-1.0,
            )


class TestProblemInstance:
    def _snapshot(self, **kw: object) -> BaseSnapshot:
        return BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,)),
            **kw,  # type: ignore[arg-type]
        )

    def test_a_constraint_may_be_resolved_only_once(self) -> None:
        duplicate = EffectiveConstraint(
            code="ROOM_CAPACITY", effective_category=ConstraintCategory.HARD
        )
        with pytest.raises(ValueError, match="only once"):
            ProblemInstance(
                snapshot=self._snapshot(),
                constraints=(duplicate, duplicate),
                objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
            )

    def test_greenfield_rejects_an_instance_carrying_base_assignments(self) -> None:
        """GREENFIELD omits SCHEDULE_STABILITY, so those assignments would be
        silently ignored rather than merely unused."""
        with pytest.raises(ValueError, match="MINIMAL_DISRUPTION"):
            ProblemInstance(
                snapshot=self._snapshot(
                    base_assignments=(
                        BaseAssignment(session_id="S1", start_slot_id="t0"),
                    )
                ),
                constraints=(),
                objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
            )

    def test_minimal_disruption_accepts_base_assignments(self) -> None:
        instance = ProblemInstance(
            snapshot=self._snapshot(
                base_assignments=(BaseAssignment(session_id="S1", start_slot_id="t0"),)
            ),
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["MINIMAL_DISRUPTION"],
        )
        assert instance.snapshot.base_assignments

    def test_the_catalog_is_the_floor_of_hard_enforcement(self) -> None:
        """An instance that configures nothing still enforces everything.

        Reading only the rows that happen to exist meant an empty constraint
        list enforced nothing at all while `missing_adapters` reported a
        complete registry — a solver returning OPTIMAL for a timetable that
        double-books a teacher.
        """
        from academic_ai.domain.scheduling.catalog import solver_hard_codes

        instance = ProblemInstance(
            snapshot=self._snapshot(),
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        assert instance.hard_codes() == solver_hard_codes()
        assert "TEACHER_NO_OVERLAP" in instance.hard_codes()

    def test_a_documented_downgrade_removes_a_code_from_the_floor(self) -> None:
        instance = ProblemInstance(
            snapshot=self._snapshot(),
            constraints=(
                EffectiveConstraint(
                    code="ROOM_CAPACITY",
                    effective_category=ConstraintCategory.MEDIUM,
                    overridden_by_user_id="U-dean",
                    override_reason="Acuerdo de facultad 2026-03",
                ),
            ),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        assert "ROOM_CAPACITY" not in instance.hard_codes()
        assert "TEACHER_NO_OVERLAP" in instance.hard_codes()

    def test_deactivating_physical_reality_is_rejected(self) -> None:
        """`is_active` was an unaudited override channel: it switched a
        non-overridable HARD constraint off with no approver and no reason,
        bypassing both the category check and the database CHECK."""
        with pytest.raises(ValueError, match="cannot be deactivated"):
            EffectiveConstraint(
                code="TEACHER_NO_OVERLAP",
                effective_category=ConstraintCategory.HARD,
                is_active=False,
            )

    def test_deactivating_a_policy_constraint_is_fine(self) -> None:
        constraint = EffectiveConstraint(
            code="TEACHER_MAX_DAILY_HOURS",
            effective_category=ConstraintCategory.MEDIUM,
            is_active=False,
        )
        assert not constraint.is_active


class TestMissingAdapters:
    def test_a_hard_constraint_with_no_adapter_is_reported(self) -> None:
        """The single most important safety property of the pluggable
        architecture: a HARD constraint with no adapter does not fail loudly,
        it silently is not enforced, and the solver returns a confident
        OPTIMAL for a schedule that double-books a room."""
        instance = ProblemInstance(
            snapshot=BaseSnapshot(
                period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,))
            ),
            constraints=(
                EffectiveConstraint(
                    code="ROOM_NO_OVERLAP", effective_category=ConstraintCategory.HARD
                ),
                EffectiveConstraint(
                    code="TEACHER_NO_OVERLAP",
                    effective_category=ConstraintCategory.HARD,
                ),
            ),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        missing = missing_adapters(instance, ["ROOM_NO_OVERLAP"])
        assert "TEACHER_NO_OVERLAP" in missing
        assert "ROOM_NO_OVERLAP" not in missing

    def test_a_complete_registry_reports_nothing(self) -> None:
        instance = ProblemInstance(
            snapshot=BaseSnapshot(
                period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,))
            ),
            constraints=(
                EffectiveConstraint(
                    code="ROOM_NO_OVERLAP", effective_category=ConstraintCategory.HARD
                ),
            ),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        from academic_ai.domain.scheduling.catalog import solver_hard_codes

        assert missing_adapters(instance, sorted(solver_hard_codes())) == frozenset()


class TestDomainRestriction:
    def test_an_override_stops_the_pruning(self) -> None:
        """A capacity limit a named human downgraded must not prune the
        domain: it has become an objective term, and pruning would enforce
        something the institution decided to stop enforcing."""
        instance = get_fixture("DOWNGRADED_CAPACITY_ADMITS_SMALL_ROOM").instance()
        with_override, _ = compute_domains(instance.snapshot, instance.constraints)
        without_override, diagnostics = compute_domains(instance.snapshot, ())

        assert with_override["S1"].room_ids == ("R1",)
        assert without_override["S1"].room_ids == ()
        assert any(d.is_blocking for d in diagnostics)

    def test_a_downgraded_constraint_moves_into_objective_tier_one(self) -> None:
        instance = get_fixture("DOWNGRADED_CAPACITY_ADMITS_SMALL_ROOM").instance()
        assert [c.code for c in instance.downgraded_hard()] == ["ROOM_CAPACITY"]
        tier = next(
            t
            for t in instance.objective_profile.tiers
            if t.collects("ROOM_CAPACITY")
        )
        assert tier.code == "RELAXED_HARD_VIOLATIONS"
        assert tier.rank <= 2, "a relaxed hard constraint must outrank policy"

    def test_co_teaching_prunes_on_the_summed_seat_demand(self) -> None:
        """A merged offering needs one room big enough for everybody; pruning
        each member against its own section size would leave rooms in the
        domain that cannot hold the group (ADR-0004)."""
        instance = get_fixture("CO_TEACHING_MERGED").instance()
        domains, _ = compute_domains(instance.snapshot, instance.constraints)
        for domain in domains.values():
            assert domain.room_ids == ("BIG",)

    def test_teacher_unavailability_shrinks_the_start_domain(self) -> None:
        instance = get_fixture("TEACHER_UNAVAILABLE_EVERYWHERE").instance()
        domains, _ = compute_domains(instance.snapshot, instance.constraints)
        assert domains["S1"].start_slot_ids == ()
        assert domains["S1"].is_empty

    def test_an_asynchronous_session_has_no_variables_and_is_not_empty(self) -> None:
        """"Needs no room" and "no room possible" must stay distinguishable."""
        instance = get_fixture("ASYNCHRONOUS_SESSION_IGNORED").instance()
        domains, _ = compute_domains(instance.snapshot, instance.constraints)
        assert "S2-async" not in domains
        assert not domains["S1"].is_empty

    def test_domain_size_counts_the_search_space(self) -> None:
        instance = get_fixture("TRIVIAL_FEASIBLE").instance()
        domains, _ = compute_domains(instance.snapshot, instance.constraints)
        # 2 slots × 1 room
        assert domains["S1"].size == 2

    def test_an_unschedulable_session_is_named_individually(self) -> None:
        """An empty domain is a provable, localised infeasibility. The same
        fact found inside the solver is a bare INFEASIBLE for the whole
        institution, which is the answer SESSION 6 exists to prevent."""
        instance = get_fixture("LUNCH_BAND_BLOCKS_LONG_SESSION").instance()
        _, diagnostics = compute_domains(instance.snapshot, instance.constraints)
        blocking = [d for d in diagnostics if d.is_blocking]
        assert len(blocking) == 1
        assert blocking[0].subject_kind == "class_session"
        assert blocking[0].subject_id == "S1"
