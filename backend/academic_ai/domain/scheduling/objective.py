"""The objective: lexicographic tiers, not a weighted sum.

Why not one weighted sum
========================

The obvious design is a single objective ``Σ wᵢ·violationᵢ`` with the weights
chosen so that no amount of tier-``k+1`` improvement can pay for a tier-``k``
regression — "weight stratification". It is rejected here for two independent
reasons.

*It does not fit.* Separation requires each weight to exceed the largest
possible total of everything below it. With per-tier magnitudes bounded by
``M`` and ``K`` tiers, the top weight is on the order of ``M^K``. A mid-sized
institution has ``M`` in the tens of thousands of student-conflict incidences,
and this model has five tiers, so the leading coefficient lands around
``10^20`` — past int64, and long past the point where CP-SAT's linear
relaxation and objective-bound pruning still do anything useful. The solver
would spend its time on arithmetic instead of search.

*The numbers would be invented.* The playbook forbids arbitrary objective
weights, and ``10^16`` is arbitrary in the purest sense: it encodes no fact
about the institution, only about the arithmetic needed to fake a priority
order. A priority order should be written down as a priority order.

How the tiers are solved
========================

Standard lexicographic optimisation, one CP-SAT solve per tier:

1. Solve for feasibility. All HARD constraints. No objective. If UNSAT, the
   answer is INFEASIBLE and no tier is ever evaluated.
2. For tier ``k`` in rank order: minimise ``f_k``, obtaining ``v_k``; then
   post ``f_k ≤ v_k + tolerance_k`` and continue to tier ``k+1``.

Each tier's solve keeps every constraint from the tiers above it, so the
result is optimal for tier ``k`` among all solutions optimal for tiers
``1..k-1``. SESSION 3 must record each ``v_k`` as an `OptimizationMetric`:
without them, "the schedule got better" is unfalsifiable, and comparing two
runs by their final objective alone is meaningless across profiles.

``tolerance`` defaults to zero — strict lexicography. A non-zero tolerance is
a real institutional decision ("we will accept up to 5 more mandatory
conflicts if it materially helps teachers"), and it is exactly the kind of
decision that must be recorded rather than defaulted into.

Within one tier
===============

Terms inside a tier are summed, and that is only legitimate because of two
rules the tests enforce:

* every term in a tier shares one `TermUnit`, so the sum is of like things;
* no weight is invented by the platform. `WeightSource` has no ``PLATFORM``
  member. Weights come from `PlanningConstraint.weight` and
  `Preference.weight`, both set by the humans who imposed the constraint or
  stated the wish, and default to 1.0 in the schema. Where SESSION 5 will
  supply per-student criticality, the source is named
  ``CRITICALITY_PROVIDER`` and behaves as uniform until that session exists.

Tier order is a policy, not a truth
===================================

Two profiles ship. They differ in one thing — where schedule stability sits —
and that difference is the whole reason profiles exist. Building a timetable
from nothing has nothing to be stable relative to; repairing a published one
has students and teachers who already planned around it. An institution that
disagrees with either order changes the profile, and the profile is recorded
on the run, so a past result stays interpretable.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from academic_ai.domain.scheduling.catalog import CONSTRAINT_CATALOG
from academic_ai.domain.enums import ConstraintCategory
from academic_ai.domain.scheduling.catalog import downgradable_hard_codes
from academic_ai.domain.scheduling.enums import TermUnit, ViolationMeasure

__all__ = [
    "OBJECTIVE_PROFILES",
    "GREENFIELD",
    "MINIMAL_DISRUPTION",
    "ObjectiveProfile",
    "ObjectiveTier",
    "TierMembership",
    "Tolerance",
]


class TierMembership(str, Enum):
    """How a tier decides which constraints belong to it."""

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    #: The tier names its constraint codes.
    EXPLICIT = "EXPLICIT"
    #: The tier collects every constraint whose catalog default is HARD but
    #: whose effective category a human downgraded. Membership is therefore
    #: known only once the effective constraint set is resolved — which is
    #: the point: a downgraded hard constraint must never fall out of the
    #: objective just because nobody remembered to list it.
    ANY_DOWNGRADED_HARD = "ANY_DOWNGRADED_HARD"


@dataclass(frozen=True, slots=True)
class Tolerance:
    """How much of tier ``k``'s optimum may be given back to help tier ``k+1``.

    The bound posted before moving on is
    ``f_k ≤ v_k + absolute + floor(relative × v_k)``.
    """

    absolute: int = 0
    relative: float = 0.0

    def __post_init__(self) -> None:
        if self.absolute < 0:
            raise ValueError("absolute tolerance must be non-negative")
        if not 0.0 <= self.relative < 1.0:
            raise ValueError("relative tolerance must be in [0, 1)")

    @property
    def is_strict(self) -> bool:
        return self.absolute == 0 and self.relative == 0.0

    def bound_for(self, optimum: int) -> int:
        """The bound to post on tier ``k`` before optimising tier ``k+1``."""
        return optimum + self.absolute + int(self.relative * optimum)


@dataclass(frozen=True, slots=True)
class ObjectiveTier:
    """One level of the lexicographic objective."""

    rank: int
    code: str
    description: str
    unit: TermUnit
    membership: TierMembership = TierMembership.EXPLICIT
    constraint_codes: tuple[str, ...] = ()
    tolerance: Tolerance = Tolerance()
    #: The dimension this tier sums. INDICATOR counts violated instances and
    #: may therefore mix constraints whose natural measure is a magnitude —
    #: the count is dimensionless, so the human weight is the only exchange
    #: rate. A magnitude-bearing tier may only contain terms of its own
    #: dimension, because summing seats with minutes invents a rate nobody
    #: agreed to.
    measure: ViolationMeasure = ViolationMeasure.INDICATOR

    def collects(self, constraint_code: str) -> bool:
        """Whether this tier takes responsibility for a constraint's term."""
        if self.membership is TierMembership.EXPLICIT:
            return constraint_code in self.constraint_codes
        spec = CONSTRAINT_CATALOG.get(constraint_code)
        if spec is None:
            return False
        # A membership tier collects only the codes it can legitimately sum,
        # which is what keeps two units out of one tier.
        return (
            spec.default_category is ConstraintCategory.HARD
            and spec.is_category_overridable
            and spec.term_unit is self.unit
        )

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError(f"{self.code}: rank must start at 1")
        if self.membership is TierMembership.EXPLICIT:
            if not self.constraint_codes:
                raise ValueError(f"{self.code}: an explicit tier must name its terms")
            for code in self.constraint_codes:
                spec = CONSTRAINT_CATALOG.get(code)
                if spec is None:
                    raise ValueError(f"{self.code}: unknown constraint code {code!r}")
                if spec.term_unit is None:
                    raise ValueError(
                        f"{self.code}: {code} contributes no objective term"
                    )
                if spec.term_unit is not self.unit:
                    raise ValueError(
                        f"{self.code}: {code} is measured in {spec.term_unit}, but "
                        f"the tier sums {self.unit}; terms in one tier must be "
                        "commensurable"
                    )
                if (
                    not self.measure.is_dimensionless
                    and spec.violation_measure is not self.measure
                ):
                    raise ValueError(
                        f"{self.code}: {code} is measured in "
                        f"{spec.violation_measure}, but the tier sums "
                        f"{self.measure}; only a dimensionless INDICATOR tier "
                        "may mix magnitudes"
                    )
        elif self.constraint_codes:
            raise ValueError(
                f"{self.code}: a {self.membership} tier must not also name codes"
            )
        else:
            # A membership tier's roster is computed, so its coherence has to
            # be checked against everything it could ever collect.
            for code in downgradable_hard_codes():
                spec = CONSTRAINT_CATALOG[code]
                if spec.term_unit is not self.unit:
                    continue
                if (
                    not self.measure.is_dimensionless
                    and spec.violation_measure is not self.measure
                ):
                    raise ValueError(
                        f"{self.code}: could collect {code}, measured in "
                        f"{spec.violation_measure}, but the tier sums "
                        f"{self.measure}"
                    )


@dataclass(frozen=True, slots=True)
class ObjectiveProfile:
    """An ordered set of tiers, plus a written reason for every omission.

    ``omitted_terms`` is not bookkeeping. A soft constraint that appears in no
    tier is a constraint the institution configured, the UI displays, and the
    solver silently ignores. Requiring a reason string makes that impossible
    to do by accident, and `tests/test_objective.py` checks that every
    objective-bearing catalog entry is either placed or explained.
    """

    code: str
    description: str
    tiers: tuple[ObjectiveTier, ...]
    omitted_terms: Mapping[str, str]

    def __post_init__(self) -> None:
        ranks = [tier.rank for tier in self.tiers]
        if ranks != list(range(1, len(self.tiers) + 1)):
            raise ValueError(f"{self.code}: tier ranks must be 1..n in order, got {ranks}")
        seen: set[str] = set()
        for tier in self.tiers:
            for code in tier.constraint_codes:
                if code in seen:
                    raise ValueError(
                        f"{self.code}: {code} appears in more than one tier; a "
                        "term counted twice is a priority order that lies"
                    )
                seen.add(code)
        for code in self.omitted_terms:
            if code in seen:
                raise ValueError(f"{self.code}: {code} is both placed and omitted")
            if not self.omitted_terms[code].strip():
                raise ValueError(f"{self.code}: omitting {code} needs a reason")

    def tier_for(self, constraint_code: str) -> ObjectiveTier | None:
        for tier in self.tiers:
            if constraint_code in tier.constraint_codes:
                return tier
        return None


# ---------------------------------------------------------------------------
# The tiers
# ---------------------------------------------------------------------------

_RELAXED_HARD_INCIDENCE = ObjectiveTier(
    rank=1,
    code="RELAXED_HARD_STUDENT_INCIDENCES",
    description=(
        "Student conflict incidences on a protection a named human "
        "downgraded — in practice STUDENT_NO_OVERLAP_UNRESOLVABLE. Its own "
        "tier, above every other relaxed-hard violation, because these are "
        "the students the platform exists to protect and an institution that "
        "chose to strand some of them should strand as few as possible."
    ),
    unit=TermUnit.STUDENT_CONFLICT_INCIDENCE,
    membership=TierMembership.ANY_DOWNGRADED_HARD,
)

_RELAXED_HARD_WEIGHTED = ObjectiveTier(
    rank=2,
    code="RELAXED_HARD_VIOLATIONS",
    description=(
        "Violations of the remaining constraints the catalog calls HARD and "
        "a named human downgraded. Ranked above policy because the override "
        "says 'if you must', not 'go ahead' — and because the alternative "
        "reading, that a downgraded hard constraint is now merely policy, is "
        "how a room ends up over capacity to save a teacher a walk. Counted "
        "as indicators so a seat overshoot is never summed against a minute."
    ),
    unit=TermUnit.WEIGHTED_VIOLATION,
    membership=TierMembership.ANY_DOWNGRADED_HARD,
    measure=ViolationMeasure.INDICATOR,
)

_STUDENT_MANDATORY = ObjectiveTier(
    rank=2,
    code="STUDENT_MANDATORY_CONFLICTS",
    description=(
        "Student conflict incidences on mandatory enrollments. Above every "
        "policy and preference term because a mandatory clash costs a student "
        "a semester, while the terms below cost someone an inconvenience."
    ),
    unit=TermUnit.STUDENT_CONFLICT_INCIDENCE,
    constraint_codes=("STUDENT_NO_OVERLAP_MANDATORY",),
)

_STABILITY = ObjectiveTier(
    rank=3,
    code="SCHEDULE_INSTABILITY",
    description=(
        "Assignments differing from the base version. Present only when "
        "repairing an existing schedule, and placed above policy because a "
        "published timetable has already been planned around: moving a class "
        "to satisfy a daily-hours policy can cost more disruption than the "
        "policy was worth."
    ),
    unit=TermUnit.CHANGED_ASSIGNMENT,
    constraint_codes=("SCHEDULE_STABILITY",),
)

#: Note what is NOT here: ROOM_CAPACITY, ROOM_CAMPUS_MATCH, ROOM_TYPE_ALLOWED
#: and STUDENT_CAMPUS_TRAVEL. All four are HARD-overridable, so undowngraded
#: they are enforced and contribute nothing, and downgraded they are already
#: collected by tier 2. Listing them here as well counted them twice — which
#: let the policy tier re-minimise a subset of relaxed-hard violations at a
#: different weighting and so silently choose *which* students to harm without
#: changing the total.
_POLICY_CODES = (
    "TEACHER_MAX_DAILY_HOURS",
    "TEACHER_MAX_CONSECUTIVE_SLOTS",
    "TEACHER_MAX_TEACHING_DAYS",
    "STUDENT_MAX_DAILY_HOURS",
    "SECTION_DAY_SPREAD",
)

_POLICY_DESCRIPTION = (
    "Institutional policy violations, weighted by whoever imposed the policy. "
    "Several of these are HARD by default and only reach this tier when they "
    "have not been downgraded — an undowngraded HARD constraint is enforced, "
    "not optimised, so it contributes nothing here. Listing them anyway is "
    "what makes the objective total when a future session changes a default."
)

_STUDENT_ELECTIVE = ObjectiveTier(
    rank=5,
    code="STUDENT_ELECTIVE_CONFLICTS",
    description=(
        "Student conflict incidences where at least one enrollment is not "
        "mandatory. Below policy because the student retains a move — "
        "dropping or deferring the elective — that no policy violation offers "
        "its victim."
    ),
    unit=TermUnit.STUDENT_CONFLICT_INCIDENCE,
    constraint_codes=("STUDENT_NO_OVERLAP_ELECTIVE",),
)

_SOFT_CODES = (
    "TEACHER_GAP_MINIMISATION",
    "STUDENT_GAP_MINIMISATION",
    "ROOM_CHANGE_MINIMISATION",
    "ROOM_CAPACITY_FIT",
    "TEACHER_PREFERENCE",
    "PROGRAM_PREFERENCE",
    "COHORT_PREFERENCE",
)

_SOFT_DESCRIPTION = (
    "Stated wishes and schedule quality. One tier rather than several: these "
    "terms all reduce to human-weighted violations, and the platform has no "
    "basis for ruling that a teacher's gap outranks a cohort's preference. An "
    "institution that does have a view expresses it in the weights, which is "
    "where a view like that belongs."
)


def _renumber(*tiers: ObjectiveTier) -> tuple[ObjectiveTier, ...]:
    """Re-rank tiers 1..n after dropping one, keeping their order."""
    out: list[ObjectiveTier] = []
    for rank, tier in enumerate(tiers, start=1):
        out.append(
            ObjectiveTier(
                rank=rank,
                code=tier.code,
                description=tier.description,
                unit=tier.unit,
                membership=tier.membership,
                constraint_codes=tier.constraint_codes,
                tolerance=tier.tolerance,
            )
        )
    return tuple(out)


def _policy_tier(rank: int) -> ObjectiveTier:
    return ObjectiveTier(
        rank=rank,
        code="POLICY_COMPLIANCE",
        description=_POLICY_DESCRIPTION,
        unit=TermUnit.WEIGHTED_VIOLATION,
        constraint_codes=_POLICY_CODES,
    )


def _soft_tier(rank: int) -> ObjectiveTier:
    return ObjectiveTier(
        rank=rank,
        code="SOFT_QUALITY",
        description=_SOFT_DESCRIPTION,
        unit=TermUnit.WEIGHTED_VIOLATION,
        constraint_codes=_SOFT_CODES,
    )


#: Building a timetable from nothing.
GREENFIELD = ObjectiveProfile(
    code="GREENFIELD",
    description=(
        "Initial construction, with no base version to be stable relative to."
    ),
    tiers=_renumber(
        _RELAXED_HARD_INCIDENCE,
        _RELAXED_HARD_WEIGHTED,
        _STUDENT_MANDATORY,
        _policy_tier(3),
        _STUDENT_ELECTIVE,
        _soft_tier(5),
    ),
    omitted_terms=MappingProxyType(
        {
            "SCHEDULE_STABILITY": (
                "There is no base version. The term would evaluate to zero and "
                "look satisfied, which is a different statement from 'this "
                "does not apply' — so it is omitted explicitly. The problem "
                "builder must reject a GREENFIELD run that carries base "
                "assignments."
            ),
            "ROOM_CAPACITY": (
                "HARD by default, so enforced rather than optimised. If a "
                "named human downgrades it, tier 2 collects it automatically."
            ),
            "ROOM_CAMPUS_MATCH": (
                "HARD by default; tier 2 collects it if downgraded."
            ),
            "ROOM_TYPE_ALLOWED": (
                "HARD by default; tier 2 collects it if downgraded."
            ),
            "STUDENT_CAMPUS_TRAVEL": (
                "HARD by default; tier 2 collects it if downgraded."
            ),
            "STUDENT_NO_OVERLAP_UNRESOLVABLE": (
                "Enforced as a HARD constraint by default. If a human "
                "downgrades it, tier 1 picks it up automatically."
            ),
        }
    ),
)

#: Repairing a schedule that people have already planned around.
MINIMAL_DISRUPTION = ObjectiveProfile(
    code="MINIMAL_DISRUPTION",
    description=(
        "Re-optimisation from a base version — SESSION 11's what-if and "
        "change-request path."
    ),
    tiers=_renumber(
        _RELAXED_HARD_INCIDENCE,
        _RELAXED_HARD_WEIGHTED,
        _STUDENT_MANDATORY,
        _STABILITY,
        _policy_tier(4),
        _STUDENT_ELECTIVE,
        _soft_tier(6),
    ),
    omitted_terms=MappingProxyType(
        {
            "ROOM_CAPACITY": (
                "HARD by default, so enforced rather than optimised. If a "
                "named human downgrades it, tier 2 collects it automatically."
            ),
            "ROOM_CAMPUS_MATCH": (
                "HARD by default; tier 2 collects it if downgraded."
            ),
            "ROOM_TYPE_ALLOWED": (
                "HARD by default; tier 2 collects it if downgraded."
            ),
            "STUDENT_CAMPUS_TRAVEL": (
                "HARD by default; tier 2 collects it if downgraded."
            ),
            "STUDENT_NO_OVERLAP_UNRESOLVABLE": (
                "Enforced as a HARD constraint by default; tier 1 collects it "
                "if downgraded."
            ),
        }
    ),
)


OBJECTIVE_PROFILES: Mapping[str, ObjectiveProfile] = MappingProxyType(
    {profile.code: profile for profile in (GREENFIELD, MINIMAL_DISRUPTION)}
)
