"""The lexicographic driver.

One CP-SAT solve per tier: minimise `f_k`, obtain `v_k`, post
`f_k ≤ v_k + tolerance_k`, move on. Each tier keeps every bound above it, so
the result is optimal for tier `k` among all solutions optimal for tiers
`1..k-1`.

Two properties are load-bearing and easy to lose:

*Every tier's optimum is recorded.* Without `v_k` per tier, "the schedule got
better" is unfalsifiable, and comparing two runs by a single final number is
meaningless across profiles. SESSION 4 is forbidden from claiming an LNS
improvement without reproducible evidence, and this is the evidence.

*A tier that does not prove optimality says so.* Posting `f_k ≤ v_k` is still
safe — the incumbent satisfies it, so nothing becomes infeasible — but the
lexicographic *claim* is false from that tier down. The run reports FEASIBLE
rather than OPTIMAL, which is the honest answer, instead of a confident
OPTIMAL that no longer means what it says.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, TYPE_CHECKING

from ortools.sat.python import cp_model

from academic_ai.domain.enums import ConstraintCategory, SolverStatus
from academic_ai.domain.scheduling.catalog import CONSTRAINT_CATALOG
from academic_ai.domain.scheduling.enums import WEIGHT_SCALE, WeightSource
from academic_ai.domain.scheduling.objective import ObjectiveTier
from academic_ai.domain.scheduling.priority import conflict_pair_weights

if TYPE_CHECKING:
    from academic_ai.optimization.solve import SolverConfig
    from academic_ai.optimization.variables import ModelVariables

__all__ = ["TierOutcome", "optimise_lexicographically", "tier_expression"]


@dataclass(frozen=True, slots=True)
class TierOutcome:
    """What one tier achieved, and whether to believe it."""

    rank: int
    code: str
    value: int
    #: OPTIMAL means this tier's minimum was proven. Anything else means the
    #: lexicographic guarantee stops holding from here down.
    status: SolverStatus
    #: The bound posted before moving to the next tier.
    bound: int
    terms: int = 0

    @property
    def is_proven(self) -> bool:
        return self.status is SolverStatus.OPTIMAL


def _student_conflict_terms(
    v: ModelVariables, wanted: str, *, use_priorities: bool
) -> list[cp_model.IntVar]:
    """Reified overlap literals, one per *distinct session pair*.

    The pair weights come from `conflict_pair_weights`, which `evaluate.py`
    also calls. That shared call is the point: these two used to be separate
    loops computing the same quantity in two layers, which is exactly how they
    came to disagree by 1000x once already.
    """
    multiplicity = conflict_pair_weights(
        v.instance,
        wanted,
        priorities=v.priorities if use_priorities else None,
        known_session_ids=v.start,
    )

    terms: list[cp_model.IntVar] = []
    for (sa, sb), count in sorted(multiplicity.items()):
        overlap = v.model.NewBoolVar(f"clash[{sa},{sb}]")
        dur_a = v.sessions[sa].duration_slots
        dur_b = v.sessions[sb].duration_slots
        before = v.model.NewBoolVar(f"before[{sa},{sb}]")
        after = v.model.NewBoolVar(f"after[{sa},{sb}]")
        v.model.Add(v.start[sa] + dur_a <= v.start[sb]).OnlyEnforceIf(before)
        v.model.Add(v.start[sa] + dur_a > v.start[sb]).OnlyEnforceIf(before.Not())
        v.model.Add(v.start[sb] + dur_b <= v.start[sa]).OnlyEnforceIf(after)
        v.model.Add(v.start[sb] + dur_b > v.start[sa]).OnlyEnforceIf(after.Not())
        # overlap == not (before or after)
        v.model.AddBoolOr([before, after, overlap])
        v.model.AddImplication(before, overlap.Not())
        v.model.AddImplication(after, overlap.Not())

        scaled = v.model.NewIntVar(0, count, f"clashcount[{sa},{sb}]")
        v.model.Add(scaled == count * overlap)
        terms.append(scaled)
    return terms


def _stability_terms(v: ModelVariables) -> list[cp_model.IntVar]:
    """One indicator per assignment differing from the base version."""
    terms: list[cp_model.IntVar] = []
    for base in v.instance.snapshot.base_assignments:
        sid = base.session_id
        if sid not in v.start_lit:
            continue
        moved = v.model.NewBoolVar(f"moved[{sid}]")
        same_slot = v.start_lit[sid].get(base.start_slot_id)
        if same_slot is None:
            v.model.Add(moved == 1)
        else:
            v.model.Add(moved == 1 - same_slot)
        terms.append(moved)
    return terms


def _tier_terms(
    v: ModelVariables, tier: ObjectiveTier
) -> tuple[list[cp_model.IntVar], list[int]]:
    """Objective terms for one tier, and their integer weights.

    Terms are only generated for constraints this instance does *not* enforce
    as HARD: an undowngraded HARD constraint is enforced, not optimised, and
    adding it here would minimise something already impossible to violate.
    """
    hard = v.instance.hard_codes()
    terms: list[cp_model.IntVar] = []
    weights: list[int] = []

    for code in CONSTRAINT_CATALOG:
        if not tier.collects(code) or code in hard:
            continue
        spec = CONSTRAINT_CATALOG[code]
        resolved = v.instance.resolve(code)
        if resolved is not None and not resolved.is_active:
            continue
        weight = int(round((resolved.weight if resolved else 1.0) * WEIGHT_SCALE))

        if code.startswith("STUDENT_NO_OVERLAP"):
            raw = _student_conflict_terms(
                v,
                code,
                use_priorities=spec.weight_source is WeightSource.CRITICALITY_PROVIDER,
            )
        elif code == "SCHEDULE_STABILITY":
            raw = _stability_terms(v)
        else:
            # Constraints whose adapters SESSION 3 has not yet written
            # contribute nothing rather than contributing wrongly. They are
            # listed in OPTIMIZER.md so the gap is visible.
            raw = []

        # Weights are carried alongside the terms rather than folded into an
        # auxiliary variable. The auxiliary version bounded that variable at
        # `weight * 1000`, which posted `weight * count <= weight * 1000` —
        # i.e. it silently capped a student-pair multiplicity at 1000
        # whatever the weight. Past the cap the tier reported 0 where the
        # truth was 1001, and an avoidable MEDIUM clash was quietly promoted
        # to HARD. It was dormant only while every weight was exactly 1.0,
        # which is the branch that bypassed the wrapper — and SESSION 5's
        # criticality provider is precisely what makes a weight non-unit.
        terms.extend(raw)
        weights.extend([weight] * len(raw))
    return terms, weights


def tier_expression(
    v: ModelVariables, tier: ObjectiveTier
) -> tuple[cp_model.LinearExpr | None, int]:
    """The weighted sum for one tier, and how many terms it has."""
    terms, weights = _tier_terms(v, tier)
    if not terms:
        return None, 0
    return cp_model.LinearExpr.WeightedSum(terms, weights), len(terms)


def optimise_lexicographically(
    v: ModelVariables,
    solver: cp_model.CpSolver,
    config: SolverConfig,
    extract: Callable[[], tuple],
) -> tuple[tuple[TierOutcome, ...], SolverStatus, tuple]:
    """Minimise each tier in rank order, bounding as it goes.

    Assumes a feasible solution already exists — `solve()` establishes that
    first, so an INFEASIBLE instance never reaches a tier and no tier optimum
    is ever reported for a schedule that does not exist.

    ``extract`` is called after every solve that actually produced a solution,
    and the last such snapshot is what the caller gets. Reading the solver
    after a solve that returned UNKNOWN yields values from no solution at all:
    that produced schedules violating teacher, room and section disjunctions
    at the same time as the run reported FEASIBLE — caught only because the
    independent validator does not trust the solver.
    """
    outcomes: list[TierOutcome] = []
    overall = SolverStatus.OPTIMAL
    best = extract()

    for tier in v.instance.objective_profile.tiers:
        total, term_count = tier_expression(v, tier)
        if total is None:
            outcomes.append(
                TierOutcome(tier.rank, tier.code, 0, SolverStatus.OPTIMAL, 0, 0)
            )
            continue

        # Try zero first. Every term is non-negative by construction, so a
        # feasible solution with total = 0 *is* the minimum, proven without an
        # optimality search. This is not a shortcut for its own sake: a
        # satisfied tier is the common case, and asking CP-SAT to close the
        # optimality gap at 0 over a hugely symmetric timetable is expensive.
        # On the small benchmark that cost 30 seconds and still came back
        # FEASIBLE; posed as a satisfiability question it is milliseconds.
        #
        # Posted under an assumption literal rather than as a plain constraint
        # because CP-SAT has no way to retract one, and a probe that could not
        # be retracted would forbid every non-zero tier for the rest of the run.
        probe = v.model.NewBoolVar(f"zero_probe[{tier.code}]")
        v.model.Add(total == 0).OnlyEnforceIf(probe)
        v.model.AddAssumption(probe)
        raw = solver.Solve(v.model)
        v.model.ClearAssumptions()

        if raw in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            best = extract()
            bound = tier.tolerance.bound_for(0)
            v.model.Add(total <= bound)
            outcomes.append(
                TierOutcome(
                    tier.rank, tier.code, 0, SolverStatus.OPTIMAL, bound, term_count
                )
            )
            continue
        if raw == cp_model.INFEASIBLE:
            # Zero is impossible given everything above, so the probe can be
            # pinned false and never explored again.
            v.model.Add(probe == 0)

        v.model.Minimize(total)
        raw = solver.Solve(v.model)

        if raw not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            # The bound from the previous tier still holds and the incumbent
            # survives; we simply stop being able to claim optimality.
            overall = SolverStatus.FEASIBLE
            outcomes.append(
                TierOutcome(
                    tier.rank, tier.code, 0, SolverStatus.UNKNOWN, 0, term_count
                )
            )
            break

        best = extract()
        value = int(solver.ObjectiveValue())
        status = (
            SolverStatus.OPTIMAL if raw == cp_model.OPTIMAL else SolverStatus.FEASIBLE
        )
        if status is not SolverStatus.OPTIMAL:
            overall = SolverStatus.FEASIBLE

        bound = tier.tolerance.bound_for(value)
        v.model.Add(total <= bound)
        outcomes.append(
            TierOutcome(tier.rank, tier.code, value, status, bound, term_count)
        )

    # `best` is the last solution the solver actually produced, so it is the
    # one whose tier values were recorded. No final re-solve: that would risk
    # returning a different schedule from the one just measured.
    return tuple(outcomes), overall, best
