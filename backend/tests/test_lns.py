"""Large Neighborhood Search.

Three things are worth defending here, and only one of them is "it makes the
schedule better":

* **A policy cannot break feasibility.** It selects session ids; CP-SAT
  decides what may replace them. That is what keeps CLAUDE.md's rule true
  once a learned policy arrives — no policy ever produces a schedule.
* **Every run is reproducible.** SESSION 4 forbids claiming an improvement
  without reproducible evidence, so a policy that consulted a global RNG or a
  clock would make every such claim unfalsifiable.
* **Improvement is measured lexicographically**, not by a scalar. Collapsing
  the tiers to one number would reintroduce the invented exchange rate the
  objective design exists to avoid.
"""

from __future__ import annotations

import pytest

from academic_ai.domain.scheduling.enums import WEIGHT_SCALE
from academic_ai.domain.scheduling.evaluate import ScoreVector, evaluate
from academic_ai.domain.scheduling.fixtures import get_fixture
from academic_ai.domain.scheduling.neighborhood import (
    Neighborhood,
    NeighborhoodPolicy,
    PlacedSession,
    SearchState,
)
from academic_ai.optimization.benchmark import BenchmarkCase, generate
from academic_ai.optimization.lns import (
    DEFAULT_POLICIES,
    POLICY_REGISTRY,
    LnsConfig,
    RandomDestroy,
    optimise,
    policy_by_code,
)
from academic_ai.optimization.solve import SolverConfig, solve

SMALL = BenchmarkCase("t", sections=12, teachers=5, rooms=5, students=60)


def _solved(case: BenchmarkCase = SMALL, seconds: float = 10.0):
    instance = generate(case)
    outcome = solve(instance, SolverConfig(time_limit_seconds=seconds))
    placement = {
        a.session_id: PlacedSession(a.session_id, a.start_slot_id, a.room_id)
        for a in outcome.assignments
    }
    return instance, placement


def _state(instance, placement, **kw) -> SearchState:
    return SearchState(
        instance=instance,
        placement=placement,
        score=evaluate(instance, placement).values,
        **kw,
    )


class TestPolicyContract:
    def test_the_playbook_s_eight_strategies_all_exist(self) -> None:
        assert set(POLICY_REGISTRY) == {
            "RANDOM",
            "HIGH_CONFLICT",
            "STUDENT_CRITICALITY",
            "TEACHER_BOTTLENECK",
            "ROOM_BOTTLENECK",
            "TIME_WINDOW",
            "PROGRAM",
            "CONFLICT_COMPONENT",
        }

    @pytest.mark.parametrize("policy", DEFAULT_POLICIES, ids=lambda p: p.code)
    def test_every_policy_satisfies_the_protocol(self, policy) -> None:
        assert isinstance(policy, NeighborhoodPolicy)

    @pytest.mark.parametrize("policy", DEFAULT_POLICIES, ids=lambda p: p.code)
    def test_a_policy_only_ever_names_placed_sessions(self, policy) -> None:
        """A neighbourhood naming something unplaced would free a session the
        repair cannot pin or move."""
        instance, placement = _solved()
        chosen = policy.select(_state(instance, placement))
        assert chosen.session_ids <= set(placement)

    @pytest.mark.parametrize("policy", DEFAULT_POLICIES, ids=lambda p: p.code)
    def test_a_policy_is_deterministic(self, policy) -> None:
        """Same state, same seed, same neighbourhood — otherwise no benchmark
        in this session can be replayed."""
        instance, placement = _solved()
        state = _state(instance, placement, iteration=3, seed=42)
        assert policy.select(state).session_ids == policy.select(state).session_ids

    @pytest.mark.parametrize("policy", DEFAULT_POLICIES, ids=lambda p: p.code)
    def test_a_policy_names_itself(self, policy) -> None:
        instance, placement = _solved()
        chosen = policy.select(_state(instance, placement))
        assert chosen.policy_code == policy.code
        assert chosen.rationale

    def test_different_seeds_can_give_different_neighbourhoods(self) -> None:
        instance, placement = _solved()
        policy = RandomDestroy()
        a = policy.select(_state(instance, placement, seed=1)).session_ids
        b = policy.select(_state(instance, placement, seed=2)).session_ids
        assert a != b

    def test_a_neighbourhood_must_name_its_policy(self) -> None:
        with pytest.raises(ValueError, match="must name the policy"):
            Neighborhood(frozenset({"S1"}), "")

    def test_unknown_policy_code_is_rejected(self) -> None:
        with pytest.raises(KeyError):
            policy_by_code("NOPE")


class TestScoring:
    def test_a_score_is_compared_lexicographically(self) -> None:
        codes = ("A", "B")
        assert ScoreVector((0, 99), codes) < ScoreVector((1, 0), codes)
        assert not ScoreVector((1, 0), codes) < ScoreVector((0, 99), codes)

    def test_tiers_with_no_adapter_are_reported_as_unevaluated(self) -> None:
        """A 0 meaning "satisfied" and a 0 meaning "not measured" are different
        facts, and only this distinguishes them."""
        instance, placement = _solved()
        score = evaluate(instance, placement)
        assert "POLICY_COMPLIANCE" in score.unevaluated
        assert "SOFT_QUALITY" in score.unevaluated

    def test_stability_counts_moves_from_the_base_version(self) -> None:
        """`counts()` is the unit figure; `named()` is in the scaled integer
        units the CP-SAT objective uses, so the two agree with
        `TierOutcome.value`."""
        fixture = get_fixture("MINIMAL_DISRUPTION_BASE")
        instance = fixture.instance()
        unchanged = {
            b.session_id: PlacedSession(b.session_id, b.start_slot_id, b.room_id)
            for b in instance.snapshot.base_assignments
        }
        assert evaluate(instance, unchanged).counts()["SCHEDULE_INSTABILITY"] == 0

        moved = dict(unchanged)
        moved["S2"] = PlacedSession("S2", "mon2", "R1")
        after = evaluate(instance, moved)
        assert after.counts()["SCHEDULE_INSTABILITY"] == 1
        assert after.named()["SCHEDULE_INSTABILITY"] == WEIGHT_SCALE

    def test_improvement_names_the_tier_that_changed(self) -> None:
        codes = ("A", "B")
        assert "A 5->3 (better)" in ScoreVector((3, 0), codes).improvement_over(
            ScoreVector((5, 0), codes)
        )


class TestEngine:
    def test_lns_never_returns_an_invalid_schedule(self) -> None:
        """CP-SAT remains the feasibility authority: whatever the policies
        propose, the result is independently validated."""
        instance, placement = _solved()
        outcome = optimise(
            instance, placement, LnsConfig(iterations=6, repair_seconds=2.0)
        )
        assert outcome.validation_ok

    def test_lns_never_makes_the_score_worse(self) -> None:
        """Acceptance is strictly improving, so the incumbent can only be
        replaced by something better."""
        instance, placement = _solved()
        outcome = optimise(
            instance, placement, LnsConfig(iterations=8, repair_seconds=2.0)
        )
        assert outcome.score <= outcome.initial_score

    def test_the_whole_run_is_reproducible(self) -> None:
        instance, placement = _solved()
        config = LnsConfig(iterations=6, repair_seconds=2.0, seed=7)
        first = optimise(instance, placement, config)
        second = optimise(instance, placement, config)
        assert first.score.values == second.score.values
        assert [r.policy for r in first.iterations] == [
            r.policy for r in second.iterations
        ]

    def test_every_iteration_records_what_the_playbook_requires(self) -> None:
        instance, placement = _solved()
        outcome = optimise(
            instance, placement, LnsConfig(iterations=4, repair_seconds=2.0)
        )
        for record in outcome.iterations:
            assert record.policy
            assert record.neighborhood_size >= 0
            assert record.repair_seconds >= 0.0
            assert isinstance(record.feasible, bool)
            assert isinstance(record.accepted, bool)
            assert record.solver_status
            assert len(record.score_before) == len(record.score_after)

    def test_it_stops_when_it_stops_improving(self) -> None:
        instance, placement = _solved()
        outcome = optimise(
            instance,
            placement,
            LnsConfig(iterations=100, repair_seconds=1.0, stall_limit=3),
        )
        assert outcome.stopped_early
        assert len(outcome.iterations) < 100

    def test_metrics_attribute_acceptances_to_policies(self) -> None:
        instance, placement = _solved()
        outcome = optimise(
            instance, placement, LnsConfig(iterations=8, repair_seconds=2.0)
        )
        metrics = outcome.metrics()
        assert metrics["lns.iterations"] == float(len(outcome.iterations))
        assert metrics["lns.accepted"] == float(outcome.accepted_count)

    def test_a_policy_that_frees_nothing_is_skipped_not_crashed(self) -> None:
        class Empty:
            code = "EMPTY"

            def select(self, state: SearchState) -> Neighborhood:
                return Neighborhood(frozenset(), self.code, "nothing")

        instance, placement = _solved()
        outcome = optimise(
            instance,
            placement,
            LnsConfig(iterations=3, repair_seconds=1.0, policies=(Empty(),)),
        )
        assert outcome.accepted_count == 0
        assert all(r.solver_status == "SKIPPED" for r in outcome.iterations)

    def test_a_policy_that_frees_everything_is_skipped(self) -> None:
        """Freeing the whole schedule is a restart, not a neighbourhood, and
        would make the comparison against plain CP-SAT meaningless."""

        class All:
            code = "ALL"

            def select(self, state: SearchState) -> Neighborhood:
                return Neighborhood(frozenset(state.placement), self.code, "all")

        instance, placement = _solved()
        outcome = optimise(
            instance,
            placement,
            LnsConfig(iterations=2, repair_seconds=1.0, policies=(All(),)),
        )
        assert all(r.solver_status == "SKIPPED" for r in outcome.iterations)


class TestImprovesOnPlainCpSat:
    """The claim SESSION 4 exists to make, with evidence.

    Kept small enough for the suite; `benchmark.compare()` runs the same
    comparison at institutional scale.
    """

    def test_lns_improves_a_conflict_heavy_schedule(self) -> None:
        case = BenchmarkCase("heavy", sections=30, teachers=8, rooms=6, students=250)
        instance, placement = _solved(case, seconds=10.0)
        before = evaluate(instance, placement)

        outcome = optimise(
            instance,
            placement,
            LnsConfig(iterations=16, repair_seconds=2.0, seed=0),
        )
        assert outcome.validation_ok
        assert outcome.score <= before, outcome.score.improvement_over(before)
        assert outcome.score < before, (
            "LNS accepted nothing on a conflict-heavy instance; the repair is "
            "probably not minimising anything"
        )


class TestScalingIsConsistent:
    """One scaling factor, everywhere.

    `evaluate.py` used to multiply by the raw weight while `objective.py`
    multiplied by `WEIGHT_SCALE`, so the two disagreed by exactly 1000x for
    any non-unit weight — and the LNS acceptance rule compares `ScoreVector`s.
    Harmless only while every weight was 1.0, which SESSION 5's criticality
    provider is precisely what changes.
    """

    def _weighted(self, weight: float):
        from academic_ai.domain.enums import ConstraintCategory, DayOfWeek
        from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
        from academic_ai.domain.scheduling.problem import (
            BaseSnapshot,
            EffectiveConstraint,
            PeriodSpec,
            ProblemInstance,
            SessionSpec,
            SlotSpec,
            StudentEnrollmentSpec,
        )

        enrollments = []
        for i in range(5):
            enrollments += [
                StudentEnrollmentSpec(
                    f"E{i}", "A", ("S1",), True, alternative_section_count=1
                ),
                StudentEnrollmentSpec(
                    f"E{i}", "B", ("S2",), True, alternative_section_count=1
                ),
            ]
        return ProblemInstance(
            snapshot=BaseSnapshot(
                period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,)),
                slots=(SlotSpec("m0", DayOfWeek.MONDAY, 0, 0, 480),),
                sessions=(
                    SessionSpec("S1", "s1", "A", "c1", "C1", 1, requires_room=False),
                    SessionSpec("S2", "s2", "B", "c2", "C1", 1, requires_room=False),
                ),
                enrollments=tuple(enrollments),
            ),
            constraints=(
                EffectiveConstraint(
                    code="STUDENT_NO_OVERLAP_MANDATORY",
                    effective_category=ConstraintCategory.MEDIUM,
                    weight=weight,
                ),
            ),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )

    @pytest.mark.parametrize("weight", [1.0, 2.0, 0.5])
    def test_score_vector_matches_tier_outcome(self, weight: float) -> None:
        instance = self._weighted(weight)
        outcome = solve(instance, SolverConfig(time_limit_seconds=10.0))
        placement = {
            a.session_id: PlacedSession(a.session_id, a.start_slot_id, a.room_id)
            for a in outcome.assignments
        }
        tier = next(t for t in outcome.tiers if "MANDATORY" in t.code)
        assert (
            evaluate(instance, placement).named()["STUDENT_MANDATORY_CONFLICTS"]
            == tier.value
        )

    def test_a_pair_multiplicity_above_1000_is_not_capped(self) -> None:
        """The auxiliary objective variable used to be bounded at
        `weight x 1000`, which posted `count <= 1000` whatever the weight.
        Past the cap the tier reported 0 where the truth was 1001 — a false
        zero in the metric a human reads to check students were protected."""
        from academic_ai.domain.enums import ConstraintCategory, DayOfWeek
        from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
        from academic_ai.domain.scheduling.problem import (
            BaseSnapshot,
            EffectiveConstraint,
            PeriodSpec,
            ProblemInstance,
            SessionSpec,
            SlotSpec,
            StudentEnrollmentSpec,
        )

        enrollments = []
        for i in range(1001):
            enrollments += [
                StudentEnrollmentSpec(
                    f"E{i}", "A", ("S1",), True, alternative_section_count=1
                ),
                StudentEnrollmentSpec(
                    f"E{i}", "B", ("S2",), True, alternative_section_count=1
                ),
            ]
        instance = ProblemInstance(
            snapshot=BaseSnapshot(
                period=PeriodSpec(id="P", slot_minutes=60, days=(DayOfWeek.MONDAY,)),
                slots=(SlotSpec("m0", DayOfWeek.MONDAY, 0, 0, 480),),
                sessions=(
                    SessionSpec("S1", "s1", "A", "c1", "C1", 1, requires_room=False),
                    SessionSpec("S2", "s2", "B", "c2", "C1", 1, requires_room=False),
                ),
                enrollments=tuple(enrollments),
            ),
            constraints=(
                EffectiveConstraint(
                    code="STUDENT_NO_OVERLAP_MANDATORY",
                    effective_category=ConstraintCategory.MEDIUM,
                    weight=2.0,
                ),
            ),
            objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
        )
        outcome = solve(instance, SolverConfig(time_limit_seconds=20.0))
        tier = next(t for t in outcome.tiers if "MANDATORY" in t.code)
        # Recomputed from `criticality()` rather than from the pair-weight
        # helper the objective uses, so the assertion is still independent of
        # the code under test. SESSION 7 wired the criticality provider into
        # this tier, so the unit is criticality-weighted incidences, not bare
        # incidences — the cap this test exists for is unaffected either way.
        from academic_ai.domain.scheduling.priority import criticality

        per_student = criticality(enrollments[0]) + criticality(enrollments[1])
        assert per_student > 0
        assert tier.value == 1001 * per_student * 2 * WEIGHT_SCALE
