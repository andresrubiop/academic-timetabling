"""The lexicographic objective.

The properties worth testing are the ones that make a priority order *mean*
something: that no term is counted twice, that every term is either placed or
explicitly excused, that terms summed together are measured in the same unit,
and that a constraint a human downgraded cannot fall out of the objective just
because nobody remembered to list it.
"""

from __future__ import annotations

import pytest

from academic_ai.domain.enums import ConstraintCategory
from academic_ai.domain.scheduling.catalog import (
    CONSTRAINT_CATALOG,
    objective_bearing_codes,
)
from academic_ai.domain.scheduling.enums import TermUnit
from academic_ai.domain.scheduling.objective import (
    GREENFIELD,
    MINIMAL_DISRUPTION,
    OBJECTIVE_PROFILES,
    ObjectiveProfile,
    ObjectiveTier,
    TierMembership,
    Tolerance,
)

PROFILES = list(OBJECTIVE_PROFILES.values())


class TestProfileStructure:
    @pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.code)
    def test_ranks_are_dense_and_ordered(self, profile: ObjectiveProfile) -> None:
        assert [t.rank for t in profile.tiers] == list(
            range(1, len(profile.tiers) + 1)
        )

    @pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.code)
    def test_no_term_appears_in_two_tiers(self, profile: ObjectiveProfile) -> None:
        seen: list[str] = []
        for tier in profile.tiers:
            seen.extend(tier.constraint_codes)
        assert len(seen) == len(set(seen)), "a term counted twice is a lying priority"

    @pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.code)
    def test_every_objective_bearing_constraint_is_placed_or_excused(
        self, profile: ObjectiveProfile
    ) -> None:
        """A soft constraint in no tier is one the institution configured, the
        UI shows, and the solver silently ignores."""
        placed = {
            code for tier in profile.tiers for code in tier.constraint_codes
        }
        collected_by_membership = {
            code
            for code, spec in CONSTRAINT_CATALOG.items()
            if spec.default_category is ConstraintCategory.HARD
            and spec.is_category_overridable
        }
        accounted = placed | set(profile.omitted_terms) | collected_by_membership
        missing = objective_bearing_codes() - accounted
        assert not missing, (
            f"{profile.code}: these contribute a term but appear in no tier "
            f"and have no written reason for omission: {sorted(missing)}"
        )

    @pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.code)
    def test_omissions_carry_a_reason(self, profile: ObjectiveProfile) -> None:
        for code, reason in profile.omitted_terms.items():
            assert reason.strip(), f"{profile.code}: omitting {code} needs a reason"
            assert code in CONSTRAINT_CATALOG

    @pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.code)
    def test_terms_within_a_tier_are_commensurable(
        self, profile: ObjectiveProfile
    ) -> None:
        """Summing unlike things invents an exchange rate nobody agreed to."""
        for tier in profile.tiers:
            for code in tier.constraint_codes:
                assert CONSTRAINT_CATALOG[code].term_unit is tier.unit, (
                    f"{profile.code}/{tier.code}: {code} is measured in "
                    f"{CONSTRAINT_CATALOG[code].term_unit}, tier sums {tier.unit}"
                )


class TestTierOrder:
    def test_downgraded_hard_constraints_rank_first(self) -> None:
        """An override says "if you must", not "go ahead"."""
        for profile in PROFILES:
            first = profile.tiers[0]
            assert first.membership is TierMembership.ANY_DOWNGRADED_HARD
            assert first.rank == 1

    def test_mandatory_student_conflicts_outrank_policy_and_preference(self) -> None:
        for profile in PROFILES:
            student = next(
                t for t in profile.tiers if "STUDENT_NO_OVERLAP_MANDATORY" in t.constraint_codes
            )
            policy = next(t for t in profile.tiers if t.code == "POLICY_COMPLIANCE")
            soft = next(t for t in profile.tiers if t.code == "SOFT_QUALITY")
            assert student.rank < policy.rank < soft.rank

    def test_mandatory_conflicts_outrank_elective_conflicts(self) -> None:
        for profile in PROFILES:
            mandatory = next(
                t for t in profile.tiers if "STUDENT_NO_OVERLAP_MANDATORY" in t.constraint_codes
            )
            elective = next(
                t for t in profile.tiers if "STUDENT_NO_OVERLAP_ELECTIVE" in t.constraint_codes
            )
            assert mandatory.rank < elective.rank, (
                "a clash the student cannot drop must outrank one they can"
            )

    def test_the_two_profiles_differ_only_in_stability(self) -> None:
        """That difference is the entire reason profiles exist."""
        greenfield_codes = {
            code for tier in GREENFIELD.tiers for code in tier.constraint_codes
        }
        repair_codes = {
            code for tier in MINIMAL_DISRUPTION.tiers for code in tier.constraint_codes
        }
        assert repair_codes - greenfield_codes == {"SCHEDULE_STABILITY"}
        assert greenfield_codes - repair_codes == set()

    def test_stability_outranks_policy_when_repairing(self) -> None:
        """A published timetable has already been planned around: moving a
        class to satisfy a daily-hours policy can cost more than the policy
        was worth."""
        stability = next(
            t for t in MINIMAL_DISRUPTION.tiers if t.code == "SCHEDULE_INSTABILITY"
        )
        policy = next(
            t for t in MINIMAL_DISRUPTION.tiers if t.code == "POLICY_COMPLIANCE"
        )
        assert stability.rank < policy.rank

    def test_stability_never_outranks_mandatory_student_conflicts(self) -> None:
        stability = next(
            t for t in MINIMAL_DISRUPTION.tiers if t.code == "SCHEDULE_INSTABILITY"
        )
        student = next(
            t
            for t in MINIMAL_DISRUPTION.tiers
            if "STUDENT_NO_OVERLAP_MANDATORY" in t.constraint_codes
        )
        assert student.rank < stability.rank, (
            "keeping a published timetable stable must never be worth costing "
            "a student their semester"
        )

    def test_greenfield_omits_stability_explicitly(self) -> None:
        assert "SCHEDULE_STABILITY" in GREENFIELD.omitted_terms


class TestTolerance:
    def test_default_is_strict_lexicography(self) -> None:
        assert Tolerance().is_strict
        for profile in PROFILES:
            for tier in profile.tiers:
                assert tier.tolerance.is_strict, (
                    f"{profile.code}/{tier.code}: a non-zero tolerance is an "
                    "institutional decision and must not be a default"
                )

    def test_bound_is_the_optimum_when_strict(self) -> None:
        assert Tolerance().bound_for(7) == 7

    def test_absolute_tolerance_widens_the_bound(self) -> None:
        assert Tolerance(absolute=3).bound_for(7) == 10

    def test_relative_tolerance_scales_with_the_optimum(self) -> None:
        assert Tolerance(relative=0.5).bound_for(10) == 15
        # Rounds down: a tolerance must never accidentally permit one more.
        assert Tolerance(relative=0.5).bound_for(7) == 10

    def test_bound_is_monotonic_in_the_optimum(self) -> None:
        tol = Tolerance(absolute=2, relative=0.1)
        bounds = [tol.bound_for(v) for v in range(0, 50)]
        assert bounds == sorted(bounds)

    def test_negative_absolute_is_rejected(self) -> None:
        with pytest.raises(ValueError):
            Tolerance(absolute=-1)

    @pytest.mark.parametrize("relative", [-0.1, 1.0, 2.0])
    def test_relative_must_be_a_proper_fraction(self, relative: float) -> None:
        """At 1.0 the bound is 2×optimum, which is not a tolerance any more."""
        with pytest.raises(ValueError):
            Tolerance(relative=relative)


class TestConstruction:
    def test_explicit_tier_must_name_terms(self) -> None:
        with pytest.raises(ValueError, match="must name its terms"):
            ObjectiveTier(
                rank=1,
                code="EMPTY",
                description="x",
                unit=TermUnit.WEIGHTED_VIOLATION,
            )

    def test_membership_tier_must_not_name_terms(self) -> None:
        with pytest.raises(ValueError, match="must not also name codes"):
            ObjectiveTier(
                rank=1,
                code="MIXED",
                description="x",
                unit=TermUnit.WEIGHTED_VIOLATION,
                membership=TierMembership.ANY_DOWNGRADED_HARD,
                constraint_codes=("ROOM_CAPACITY",),
            )

    def test_unknown_constraint_code_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown constraint code"):
            ObjectiveTier(
                rank=1,
                code="T",
                description="x",
                unit=TermUnit.WEIGHTED_VIOLATION,
                constraint_codes=("NO_SUCH_CONSTRAINT",),
            )

    def test_incommensurable_terms_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="commensurable"):
            ObjectiveTier(
                rank=1,
                code="T",
                description="x",
                unit=TermUnit.WEIGHTED_VIOLATION,
                constraint_codes=("STUDENT_NO_OVERLAP_MANDATORY",),
            )

    def test_a_constraint_with_no_term_cannot_be_a_tier_member(self) -> None:
        with pytest.raises(ValueError, match="contributes no objective term"):
            ObjectiveTier(
                rank=1,
                code="T",
                description="x",
                unit=TermUnit.WEIGHTED_VIOLATION,
                constraint_codes=("TEACHER_NO_OVERLAP",),
            )

    def test_rank_starts_at_one(self) -> None:
        with pytest.raises(ValueError, match="rank must start at 1"):
            ObjectiveTier(
                rank=0,
                code="T",
                description="x",
                unit=TermUnit.WEIGHTED_VIOLATION,
                constraint_codes=("ROOM_CAPACITY",),
            )

    def test_a_term_cannot_be_both_placed_and_omitted(self) -> None:
        tier = ObjectiveTier(
            rank=1,
            code="T",
            description="x",
            unit=TermUnit.WEIGHTED_VIOLATION,
            constraint_codes=("ROOM_CAPACITY",),
        )
        with pytest.raises(ValueError, match="both placed and omitted"):
            ObjectiveProfile(
                code="BROKEN",
                description="x",
                tiers=(tier,),
                omitted_terms={"ROOM_CAPACITY": "because"},
            )

    def test_tier_lookup(self) -> None:
        assert GREENFIELD.tier_for("STUDENT_NO_OVERLAP_MANDATORY") is not None
        assert GREENFIELD.tier_for("TEACHER_NO_OVERLAP") is None
