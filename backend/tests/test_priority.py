"""`ConflictPriorityProvider`: the third AI contract, and its wiring.

Two things are worth testing here and they are different.

The **contract** — that a provider cannot zero a student out, cannot answer
about an instance it was not given, and cannot return a number large enough to
break the tier arithmetic. These are the guardrails that make it safe to let a
learned model supply objective weights at all.

The **wiring** — that the numbers actually reach CP-SAT. `WeightSource.
CRITICALITY_PROVIDER` sat in the catalog for four sessions being read by
nothing, which is the failure mode a declared extension point invites: it
looks wired, it type-checks, and it changes nothing.
"""

from __future__ import annotations

import pytest

from academic_ai.domain.enums import ConstraintCategory, DayOfWeek
from academic_ai.domain.scheduling.evaluate import evaluate
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
from academic_ai.domain.scheduling.priority import (
    MAX_PRIORITY,
    MIN_PRIORITY,
    ConflictPriorityProvider,
    HeuristicConflictPriority,
    PriorityRequest,
    conflict_pair_weights,
    criticality,
    resolve_priorities,
    validate_priorities,
)
from academic_ai.domain.scheduling.problem import (
    BaseSnapshot,
    EffectiveConstraint,
    PeriodSpec,
    ProblemInstance,
    SessionSpec,
    SlotSpec,
    StudentEnrollmentSpec,
)
from academic_ai.optimization import SolverConfig, solve

_MON = DayOfWeek.MONDAY


def _instance(
    enrollments: tuple[StudentEnrollmentSpec, ...],
    *,
    slots: int = 1,
    sessions: tuple[str, ...] = ("S1", "S2"),
) -> ProblemInstance:
    return ProblemInstance(
        snapshot=BaseSnapshot(
            period=PeriodSpec(id="P", slot_minutes=60, days=(_MON,)),
            slots=tuple(
                SlotSpec(f"m{i}", _MON, i, i, (8 + i) * 60) for i in range(slots)
            ),
            sessions=tuple(
                SessionSpec(
                    s, f"{s}-sec", s, f"{s}-crs", "C1", 1, requires_room=False
                )
                for s in sessions
            ),
            enrollments=enrollments,
        ),
        constraints=(
            EffectiveConstraint(
                code="STUDENT_NO_OVERLAP_MANDATORY",
                effective_category=ConstraintCategory.MEDIUM,
            ),
        ),
        objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
    )


def _clashing(student: str, **overrides) -> tuple[StudentEnrollmentSpec, ...]:
    """One student who needs both S1 and S2, with a way out of each."""
    kwargs = {"is_mandatory": True, "alternative_section_count": 1}
    kwargs.update(overrides)
    return (
        StudentEnrollmentSpec(student, "A", ("S1",), **kwargs),
        StudentEnrollmentSpec(student, "B", ("S2",), **kwargs),
    )


class _Fixed:
    """A provider that returns whatever it is told to. Stands in for a model."""

    code = "TEST_FIXED"

    def __init__(self, values: dict[tuple[str, str], int]) -> None:
        self.values = values
        self.calls = 0

    def priorities(self, request: PriorityRequest) -> dict[tuple[str, str], int]:
        self.calls += 1
        return dict(self.values)


class TestTheContract:
    def test_the_heuristic_satisfies_the_protocol(self) -> None:
        assert isinstance(HeuristicConflictPriority(), ConflictPriorityProvider)

    def test_a_zero_priority_is_refused(self) -> None:
        """The safety property. A zero-weight term contributes nothing, so a
        model returning zero would leave the optimizer content to strand that
        student — a constraint switched off by arithmetic."""
        instance = _instance(_clashing("E1"))
        request = PriorityRequest(instance)
        with pytest.raises(ValueError, match="removes the student"):
            validate_priorities({("E1", "A"): 0}, request)

    def test_a_negative_priority_is_refused(self) -> None:
        """Worse than zero: it would pay the objective to *cause* the clash."""
        request = PriorityRequest(_instance(_clashing("E1")))
        with pytest.raises(ValueError, match="removes the student"):
            validate_priorities({("E1", "A"): -5}, request)

    def test_a_priority_above_the_cap_is_refused(self) -> None:
        request = PriorityRequest(_instance(_clashing("E1")))
        with pytest.raises(ValueError, match="MAX_PRIORITY"):
            validate_priorities({("E1", "A"): MAX_PRIORITY + 1}, request)

    def test_a_float_priority_is_refused(self) -> None:
        """Floating point in a CP-SAT objective makes the tier value
        irreproducible, and SESSION 4 forbids unreproducible evidence."""
        request = PriorityRequest(_instance(_clashing("E1")))
        with pytest.raises(ValueError, match="integers"):
            validate_priorities({("E1", "A"): 1.5}, request)  # type: ignore[dict-item]

    def test_a_bool_is_not_an_integer_here(self) -> None:
        """`isinstance(True, int)` is True in Python, so a provider returning
        booleans would sail through a naive check and weight every student 1."""
        request = PriorityRequest(_instance(_clashing("E1")))
        with pytest.raises(ValueError, match="integers"):
            validate_priorities({("E1", "A"): True}, request)  # type: ignore[dict-item]

    def test_a_provider_cannot_answer_about_an_instance_it_was_not_given(
        self,
    ) -> None:
        request = PriorityRequest(_instance(_clashing("E1")))
        with pytest.raises(ValueError, match="not in the request"):
            validate_priorities({("GHOST", "A"): 10}, request)

    def test_an_omitted_enrollment_falls_back_rather_than_vanishing(self) -> None:
        """Omission is the other way a model could zero somebody out, and it
        is harder to see than returning zero."""
        instance = _instance(_clashing("E1") + _clashing("E2"))
        provider = _Fixed({("E1", "A"): 500})
        resolved = resolve_priorities(PriorityRequest(instance), provider)

        assert resolved[("E1", "A")] == 500
        assert set(resolved) == {
            ("E1", "A"),
            ("E1", "B"),
            ("E2", "A"),
            ("E2", "B"),
        }
        assert all(v >= MIN_PRIORITY for v in resolved.values())

    def test_no_provider_means_the_heuristic(self) -> None:
        instance = _instance(_clashing("E1"))
        resolved = resolve_priorities(PriorityRequest(instance))
        expected = criticality(instance.snapshot.enrollments[0])
        assert resolved[("E1", "A")] == expected

    def test_focus_narrows_what_the_provider_is_asked_about(self) -> None:
        instance = _instance(_clashing("E1") + _clashing("E2"))
        request = PriorityRequest(instance, focus_student_ids=frozenset({"E2"}))
        assert {e.student_id for e in request.enrollments()} == {"E2"}


class TestPairWeightsAreAdditive:
    def test_two_students_sharing_a_pair_sum(self) -> None:
        """The identity the CP-SAT encoding rests on. One reified Boolean per
        distinct pair with an integer weight is only valid if the weight is
        the sum over the students who need both."""
        one = _instance(_clashing("E1"))
        two = _instance(_clashing("E1") + _clashing("E2"))
        key = ("S1", "S2")

        w_one = conflict_pair_weights(one, "STUDENT_NO_OVERLAP_MANDATORY")[key]
        w_two = conflict_pair_weights(two, "STUDENT_NO_OVERLAP_MANDATORY")[key]
        assert w_two == 2 * w_one

    def test_priorities_weight_the_pair_by_both_enrollments(self) -> None:
        """The harm of a clash is a property of both enrollments, not of
        whichever one happens to be listed first."""
        instance = _instance(_clashing("E1"))
        weights = conflict_pair_weights(
            instance,
            "STUDENT_NO_OVERLAP_MANDATORY",
            priorities={("E1", "A"): 7, ("E1", "B"): 11},
        )
        assert weights[("S1", "S2")] == 18

    def test_without_priorities_it_is_a_plain_incidence_count(self) -> None:
        instance = _instance(_clashing("E1") + _clashing("E2") + _clashing("E3"))
        weights = conflict_pair_weights(instance, "STUDENT_NO_OVERLAP_MANDATORY")
        assert weights[("S1", "S2")] == 3

    def test_a_student_with_fewer_alternatives_outranks_one_with_more(self) -> None:
        """What ranking *within* a category means, and the only thing
        criticality claims to measure."""
        stuck = _instance(_clashing("E1", alternative_section_count=0))
        loose = _instance(_clashing("E1", alternative_section_count=3))
        p_stuck = resolve_priorities(PriorityRequest(stuck))[("E1", "A")]
        p_loose = resolve_priorities(PriorityRequest(loose))[("E1", "A")]
        assert p_stuck > p_loose


class TestTheWiringIsReal:
    """`WeightSource.CRITICALITY_PROVIDER` was declared and read by nothing."""

    def test_the_tier_value_reflects_criticality_not_a_bare_count(self) -> None:
        instance = _instance(_clashing("E1"), slots=1)
        outcome = solve(instance, SolverConfig(time_limit_seconds=15.0))
        tier = next(t for t in outcome.tiers if "MANDATORY" in t.code)

        from academic_ai.domain.scheduling.enums import WEIGHT_SCALE

        both = criticality(instance.snapshot.enrollments[0]) + criticality(
            instance.snapshot.enrollments[1]
        )
        assert tier.value == both * WEIGHT_SCALE
        assert tier.value != 1 * WEIGHT_SCALE, (
            "the tier is still counting incidences; the provider is not wired"
        )

    def test_a_provider_changes_the_tier_value(self) -> None:
        """End to end: a model's numbers reach CP-SAT and move the objective."""
        instance = _instance(_clashing("E1"), slots=1)
        provider = _Fixed({("E1", "A"): 3, ("E1", "B"): 4})
        outcome = solve(
            instance,
            SolverConfig(time_limit_seconds=15.0),
            priority_provider=provider,
        )
        tier = next(t for t in outcome.tiers if "MANDATORY" in t.code)

        from academic_ai.domain.scheduling.enums import WEIGHT_SCALE

        assert provider.calls == 1, "the provider must be asked exactly once"
        assert tier.value == 7 * WEIGHT_SCALE

    def test_the_scorer_agrees_with_the_solver_under_a_provider(self) -> None:
        """The disagreement that has already happened twice in this project.
        `evaluate()` and the CP-SAT objective must produce the same number for
        the same schedule, or every LNS acceptance decision is meaningless."""
        instance = _instance(_clashing("E1"), slots=1)
        provider = _Fixed({("E1", "A"): 3, ("E1", "B"): 4})
        outcome = solve(
            instance,
            SolverConfig(time_limit_seconds=15.0),
            priority_provider=provider,
        )
        placement = {
            a.session_id: PlacedSession(a.session_id, a.start_slot_id, a.room_id)
            for a in outcome.assignments
        }
        priorities = resolve_priorities(PriorityRequest(instance), provider)
        scored = evaluate(instance, placement, priorities=priorities)
        tier = next(t for t in outcome.tiers if "MANDATORY" in t.code)
        assert scored.named()["STUDENT_MANDATORY_CONFLICTS"] == tier.value

    def test_scoring_with_the_wrong_priorities_gives_a_different_answer(
        self,
    ) -> None:
        """Why `evaluate()` takes the priorities rather than resolving its
        own: doing so would score the incumbent under one objective and the
        candidate under another, and LNS would accept on the difference
        between two rulers."""
        instance = _instance(_clashing("E1"), slots=1)
        placement = {
            "S1": PlacedSession("S1", "m0"),
            "S2": PlacedSession("S2", "m0"),
        }
        provider = _Fixed({("E1", "A"): 3, ("E1", "B"): 4})
        with_model = evaluate(
            instance,
            placement,
            priorities=resolve_priorities(PriorityRequest(instance), provider),
        )
        with_heuristic = evaluate(instance, placement)
        assert (
            with_model.named()["STUDENT_MANDATORY_CONFLICTS"]
            != with_heuristic.named()["STUDENT_MANDATORY_CONFLICTS"]
        )

    def test_the_destroy_policy_reads_the_same_priorities(self) -> None:
        """A destroy policy reading `criticality()` directly would rank
        students by the heuristic while the objective ranked them by the
        model, and the search would free sessions the objective was not
        scoring."""
        from academic_ai.domain.scheduling.neighborhood import SearchState
        from academic_ai.optimization.lns.destroy import StudentCriticalityDestroy

        instance = _instance(
            _clashing("E1", alternative_section_count=3)
            + (
                StudentEnrollmentSpec("E2", "C", ("S3",), True,
                                      alternative_section_count=3),
            ),
            slots=3,
            sessions=("S1", "S2", "S3"),
        )
        placement = {
            s: PlacedSession(s, f"m{i}") for i, s in enumerate(("S1", "S2", "S3"))
        }
        policy = StudentCriticalityDestroy(fraction=0.34)

        # Every student here has three alternatives, so the heuristic ranks
        # all three sessions equally and the tie breaks on session id.
        heuristic = policy.select(
            SearchState(instance=instance, placement=placement)
        )
        assert "S3" not in heuristic.session_ids

        # The model says E2 is the one who cannot move. The policy must
        # follow the objective it is searching against, not its own opinion.
        with_model = policy.select(
            SearchState(
                instance=instance,
                placement=placement,
                priorities={("E2", "C"): MAX_PRIORITY},
            )
        )
        assert "S3" in with_model.session_ids
