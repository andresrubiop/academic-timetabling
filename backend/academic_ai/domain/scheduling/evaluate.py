"""Score a schedule against its objective profile, without a solver.

LNS needs to decide whether a repaired schedule is better than the incumbent,
and it needs to decide that thousands of times. Asking CP-SAT to re-derive the
objective each time would be both slow and circular — the search would be
judged by the same model that produced it.

So the score is computed here, from the assignments and the domain data alone.
That makes it usable by the LNS accept/reject rule, by SESSION 6's impact
analysis, and by any test that wants to assert an objective value without
running a solver.

**A score is a vector, not a number.** The objective is lexicographic
(ADR-0010), so comparison is tuple comparison, best-first by tier rank.
Collapsing it to a scalar would reintroduce exactly the invented exchange rate
between tiers that the lexicographic design exists to avoid.

`unevaluated` names the tiers this module cannot compute yet, because the
corresponding objective adapters do not exist. Those tiers score 0 — and a 0
that means "satisfied" is indistinguishable from a 0 that means "not measured"
unless something says which, so `unevaluated` says which.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from academic_ai.domain.enums import ConstraintCategory
from academic_ai.domain.scheduling.catalog import CONSTRAINT_CATALOG
from academic_ai.domain.scheduling.enums import WEIGHT_SCALE, WeightSource
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.domain.scheduling.priority import (
    PriorityRequest,
    conflict_pair_weights,
    resolve_priorities,
)
from academic_ai.domain.scheduling.problem import (
    ProblemInstance,
    classify_student_conflict,
)

__all__ = ["ScoreVector", "evaluate", "occupied_cells"]

#: Tier terms this module knows how to compute. Everything else scores 0 and
#: is reported through `ScoreVector.unevaluated`.
_COMPUTABLE = frozenset(
    {
        "STUDENT_NO_OVERLAP_UNRESOLVABLE",
        "STUDENT_NO_OVERLAP_MANDATORY",
        "STUDENT_NO_OVERLAP_ELECTIVE",
        "SCHEDULE_STABILITY",
    }
)


@dataclass(frozen=True, slots=True)
class ScoreVector:
    """Tier values, best-first, plus what could not be measured.

    Values are in the same scaled integer units the CP-SAT objective uses
    (`WEIGHT_SCALE` per unit weight), so a `ScoreVector` entry and the matching
    `TierOutcome.value` are directly comparable. Use `counts()` for the
    human-readable figure.
    """

    values: tuple[int, ...]
    tier_codes: tuple[str, ...]
    unevaluated: frozenset[str] = frozenset()

    def __lt__(self, other: ScoreVector) -> bool:
        """Lexicographic: tier 1 dominates absolutely."""
        return self.values < other.values

    def __le__(self, other: ScoreVector) -> bool:
        return self.values <= other.values

    @property
    def total_measured(self) -> int:
        """Sum across tiers. For logging only — never for comparison, because
        adding tiers together is precisely what lexicography forbids."""
        return sum(self.values)

    def named(self) -> Mapping[str, int]:
        return dict(zip(self.tier_codes, self.values))

    def counts(self) -> Mapping[str, float]:
        """Tier values in unit terms — incidences, moved assignments."""
        return {
            code: value / WEIGHT_SCALE
            for code, value in zip(self.tier_codes, self.values)
        }

    def improvement_over(self, other: ScoreVector) -> str:
        """Which tier changed first, for the iteration log."""
        for code, mine, theirs in zip(self.tier_codes, self.values, other.values):
            if mine != theirs:
                direction = "better" if mine < theirs else "worse"
                return f"{code} {theirs}->{mine} ({direction})"
        return "unchanged"


def occupied_cells(
    instance: ProblemInstance, placed: PlacedSession
) -> frozenset[tuple[str, int]]:
    """(day, in-day index) pairs a placement covers.

    In-day rather than week-linear, for the reason ADR-0006 gives: the
    arithmetic runs out of slots at the end of a day, so nothing can be found
    to straddle midnight.
    """
    slots = {s.id: s for s in instance.snapshot.slots}
    sessions = {s.id: s for s in instance.snapshot.sessions}
    slot = slots.get(placed.start_slot_id)
    session = sessions.get(placed.session_id)
    if slot is None or session is None:
        return frozenset()
    return frozenset(
        (slot.day_of_week.value, slot.in_day_index + offset)
        for offset in range(session.duration_slots)
    )


def _student_incidences(
    instance: ProblemInstance,
    placement: Mapping[str, PlacedSession],
    wanted: str,
    priorities: Mapping[tuple[str, str], int] | None,
) -> int:
    """Weighted (student, session-pair) collisions of one classification.

    Incidences rather than pairs: a clash between two sessions that forty
    students share is forty times the harm of one two students share, and the
    tier's unit says so.

    The pair weights come from `conflict_pair_weights` — the same call the
    CP-SAT objective makes. This function then keeps only the pairs that
    actually collide in ``placement``. Sharing the weighting rather than
    reimplementing it is the whole point: an incidence count computed twice in
    two layers is how the objective and this scorer came to disagree by 1000x,
    which made every LNS acceptance decision meaningless while both sides
    looked correct.
    """
    cells = {sid: occupied_cells(instance, p) for sid, p in placement.items()}
    weights = conflict_pair_weights(
        instance, wanted, priorities=priorities, known_session_ids=cells
    )
    return sum(
        weight for (sa, sb), weight in weights.items() if cells[sa] & cells[sb]
    )


def _moved(
    instance: ProblemInstance, placement: Mapping[str, PlacedSession]
) -> int:
    moved = 0
    for base in instance.snapshot.base_assignments:
        current = placement.get(base.session_id)
        if current is None:
            continue
        if current.start_slot_id != base.start_slot_id or (
            base.room_id is not None and current.room_id != base.room_id
        ):
            moved += 1
    return moved


def evaluate(
    instance: ProblemInstance,
    placement: Mapping[str, PlacedSession] | Sequence[PlacedSession],
    *,
    priorities: Mapping[tuple[str, str], int] | None = None,
) -> ScoreVector:
    """The objective value of a schedule, tier by tier.

    ``priorities`` must be the *same* mapping the model was built with —
    `ModelVariables.priorities`. Passing a different one, or omitting it after
    a run used a learned provider, compares two schedules under two different
    objectives and silently produces a meaningless verdict, which is precisely
    what the LNS acceptance rule would then act on.
    """
    if not isinstance(placement, Mapping):
        placement = {p.session_id: p for p in placement}
    if priorities is None:
        priorities = resolve_priorities(PriorityRequest(instance))

    hard = instance.hard_codes()
    values: list[int] = []
    codes: list[str] = []
    unevaluated: set[str] = set()

    for tier in instance.objective_profile.tiers:
        total = 0
        measured_any = False
        missing_any = False

        for code, spec in CONSTRAINT_CATALOG.items():
            if not tier.collects(code) or code in hard:
                continue
            resolved = instance.resolve(code)
            if resolved is not None and not resolved.is_active:
                continue
            if code not in _COMPUTABLE:
                missing_any = True
                continue

            measured_any = True
            # `WEIGHT_SCALE`, the same factor `objective.py` applies. These
            # two used to disagree by exactly 1000x for any non-unit weight —
            # "the second scaling factor elsewhere in the stack" that makes
            # two runs incomparable while both look correct. The LNS
            # acceptance rule compares ScoreVectors, so they must agree.
            weight = int(round((resolved.weight if resolved else 1.0) * WEIGHT_SCALE))
            if code.startswith("STUDENT_NO_OVERLAP"):
                raw = _student_incidences(
                    instance,
                    placement,
                    code,
                    priorities
                    if spec.weight_source is WeightSource.CRITICALITY_PROVIDER
                    else None,
                )
            else:  # SCHEDULE_STABILITY
                raw = _moved(instance, placement)
            total += raw * weight

        if missing_any and not measured_any:
            unevaluated.add(tier.code)
        values.append(total)
        codes.append(tier.code)

    return ScoreVector(
        values=tuple(values),
        tier_codes=tuple(codes),
        unevaluated=frozenset(unevaluated),
    )
