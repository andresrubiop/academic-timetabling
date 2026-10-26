"""The LNS loop: destroy, repair with CP-SAT, evaluate, accept or reject.

CP-SAT stays the feasibility authority (CLAUDE.md). A policy chooses *which*
sessions to free; the solver decides what may replace them. No policy — not a
heuristic here, not a learned one in SESSION 7 — can produce a schedule that
violates a HARD constraint, because no policy ever produces a schedule.

Repair by assumption, not by rebuild
====================================

The model is built **once**. Each iteration pins the sessions outside the
neighbourhood using CP-SAT assumption literals, solves, then clears them. On
the real 399-activity instance a rebuild costs ~0.4s, which would dominate a
loop of short repairs and make the benchmark measure model construction rather
than search.

Assumptions also give the honest answer when a neighbourhood cannot be
repaired: an INFEASIBLE under assumptions means *this* neighbourhood has no
better arrangement, not that the instance is broken. The incumbent survives,
the iteration is recorded as rejected, and the loop continues.

Acceptance
==========

Strictly improving, compared lexicographically by tier (ADR-0010). No
simulated annealing and no sideways moves: the playbook forbids claiming an
improvement without reproducible evidence, and the first thing to establish is
that LNS beats plain CP-SAT at all. A stochastic acceptance rule would make
that comparison a matter of luck, and is a change to make deliberately once
there is a baseline to compare against.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from ortools.sat.python import cp_model

from academic_ai.domain.enums import SolverStatus
from academic_ai.domain.scheduling.evaluate import ScoreVector, evaluate
from academic_ai.domain.scheduling.neighborhood import (
    Neighborhood,
    NeighborhoodPolicy,
    PlacedSession,
    SearchState,
)
from academic_ai.domain.scheduling.priority import ConflictPriorityProvider
from academic_ai.domain.scheduling.problem import ProblemInstance
from academic_ai.optimization.adapters import apply_hard_constraints
from academic_ai.optimization.lns.destroy import DEFAULT_POLICIES
from academic_ai.optimization.objective import tier_expression
from academic_ai.optimization.solve import SolverConfig
from academic_ai.optimization.validator import Assignment, validate
from academic_ai.optimization.variables import ModelVariables, build_variables

__all__ = ["IterationRecord", "LnsConfig", "LnsOutcome", "optimise"]


@dataclass(frozen=True, slots=True)
class LnsConfig:
    iterations: int = 40
    #: Budget for one repair. Short on purpose: LNS gets its value from many
    #: small searches, and a long limit turns the loop into a handful of
    #: near-complete solves.
    repair_seconds: float = 5.0
    seed: int = 0
    #: Stop early when this many consecutive iterations change nothing. A run
    #: that has stopped improving is spending budget to produce identical
    #: numbers.
    stall_limit: int = 12
    policies: tuple[NeighborhoodPolicy, ...] = DEFAULT_POLICIES


@dataclass(frozen=True, slots=True)
class IterationRecord:
    """Everything the playbook requires be tracked, per iteration."""

    iteration: int
    policy: str
    neighborhood_size: int
    score_before: tuple[int, ...]
    score_after: tuple[int, ...]
    repair_seconds: float
    feasible: bool
    accepted: bool
    solver_status: str
    rationale: str = ""

    @property
    def improved(self) -> bool:
        return self.score_after < self.score_before


@dataclass(frozen=True, slots=True)
class LnsOutcome:
    placement: Mapping[str, PlacedSession]
    score: ScoreVector
    initial_score: ScoreVector
    iterations: tuple[IterationRecord, ...] = ()
    wall_seconds: float = 0.0
    validation_ok: bool = True
    stopped_early: bool = False

    @property
    def accepted_count(self) -> int:
        return sum(1 for r in self.iterations if r.accepted)

    @property
    def improved(self) -> bool:
        return self.score < self.initial_score

    def metrics(self) -> Mapping[str, float]:
        by_policy: dict[str, int] = {}
        for record in self.iterations:
            if record.accepted:
                by_policy[record.policy] = by_policy.get(record.policy, 0) + 1
        out: dict[str, float] = {
            "lns.iterations": float(len(self.iterations)),
            "lns.accepted": float(self.accepted_count),
            "lns.wall_seconds": self.wall_seconds,
        }
        for code, count in by_policy.items():
            out[f"lns.accepted.{code}"] = float(count)
        for code, value in self.score.named().items():
            out[f"tier.{code}.value"] = float(value)
        return out


def _placement_from(
    v: ModelVariables, solver: cp_model.CpSolver
) -> dict[str, PlacedSession]:
    out: dict[str, PlacedSession] = {}
    for session_id, lits in v.start_lit.items():
        slot_id = next(
            (sid for sid, lit in lits.items() if solver.BooleanValue(lit)), None
        )
        if slot_id is None:  # pragma: no cover - ExactlyOne guarantees one
            continue
        room_id = next(
            (
                rid
                for rid, lit in v.room_lit.get(session_id, {}).items()
                if solver.BooleanValue(lit)
            ),
            None,
        )
        out[session_id] = PlacedSession(session_id, slot_id, room_id)
    return out


def _pin_outside(
    v: ModelVariables, placement: Mapping[str, PlacedSession], freed: frozenset[str]
) -> int:
    """Assume the current placement for everything not being re-decided."""
    pinned = 0
    for session_id, placed in placement.items():
        if session_id in freed:
            continue
        lits = v.start_lit.get(session_id)
        if lits and placed.start_slot_id in lits:
            v.model.AddAssumption(lits[placed.start_slot_id])
            pinned += 1
        room_lits = v.room_lit.get(session_id)
        if placed.room_id and room_lits and placed.room_id in room_lits:
            v.model.AddAssumption(room_lits[placed.room_id])
    return pinned


def optimise(
    instance: ProblemInstance,
    initial: Mapping[str, PlacedSession] | Sequence[PlacedSession],
    config: LnsConfig | None = None,
    solver_config: SolverConfig | None = None,
    *,
    priority_provider: ConflictPriorityProvider | None = None,
) -> LnsOutcome:
    """Improve a feasible schedule by repeatedly re-deciding part of it.

    ``initial`` must already be feasible — LNS repairs, it does not construct.
    `solve()` produces the starting point, or an import supplies one.

    The acceptance rule compares `ScoreVector`s, so both sides of every
    comparison are scored with `variables.priorities` — the same numbers the
    model was built from. Letting `evaluate()` resolve its own would score the
    incumbent under one objective and the candidate under another, and the
    loop would accept and reject on the difference between two rulers.
    """
    config = config or LnsConfig()
    solver_config = solver_config or SolverConfig()
    if not isinstance(initial, Mapping):
        initial = {p.session_id: p for p in initial}

    started = time.monotonic()
    variables = build_variables(instance, priority_provider=priority_provider)
    apply_hard_constraints(variables)

    # Objective expressions, built once. Without them the repair is a pure
    # satisfiability question, and CP-SAT answers it by handing back the first
    # feasible arrangement it finds — which, with everything outside the
    # neighbourhood pinned, is very often the incumbent unchanged. That was
    # the first version of this loop: 0 of 12 iterations accepted, not because
    # the schedule was optimal but because nothing had asked for better.
    tier_expressions: dict[str, object] = {}
    for tier in instance.objective_profile.tiers:
        expression, _ = tier_expression(variables, tier)
        if expression is not None:
            tier_expressions[tier.code] = expression

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = config.repair_seconds
    solver.parameters.num_workers = solver_config.workers
    solver.parameters.random_seed = solver_config.random_seed

    incumbent = dict(initial)
    initial_score = evaluate(instance, incumbent, priorities=variables.priorities)
    best_score = initial_score
    records: list[IterationRecord] = []
    freed_history: list[frozenset[str]] = []
    stalled = 0
    stopped_early = False

    for iteration in range(config.iterations):
        policy = config.policies[iteration % len(config.policies)]
        state = SearchState(
            instance=instance,
            placement=incumbent,
            iteration=iteration,
            seed=config.seed,
            score=best_score.values,
            # What the last few iterations already tried, so a policy can
            # avoid proposing the same neighbourhood until the budget runs out.
            recently_freed=frozenset().union(*freed_history[-3:])
            if freed_history
            else frozenset(),
            priorities=variables.priorities,
        )
        neighborhood: Neighborhood = policy.select(state)
        score_before = best_score

        if neighborhood.is_empty or neighborhood.size >= len(incumbent):
            # Freeing nothing cannot improve anything; freeing everything is
            # not a neighbourhood, it is a restart, and would make the
            # comparison against plain CP-SAT meaningless.
            records.append(
                IterationRecord(
                    iteration,
                    policy.code,
                    neighborhood.size,
                    score_before.values,
                    score_before.values,
                    0.0,
                    feasible=True,
                    accepted=False,
                    solver_status="SKIPPED",
                    rationale=neighborhood.rationale,
                )
            )
            stalled += 1
            continue

        freed_history.append(neighborhood.session_ids)

        # Aim the repair at the highest-ranked tier that is currently non-zero:
        # improving it dominates every tier below it, and the tiers above are
        # already at zero. Whether the result is actually an improvement is
        # decided afterwards by the full lexicographic comparison, so a repair
        # that trades a higher tier for a lower one is simply rejected rather
        # than needing to be forbidden here.
        target = None
        for code, value in zip(score_before.tier_codes, score_before.values):
            if value > 0 and code in tier_expressions:
                target = code
                break

        repair_started = time.monotonic()
        variables.model.ClearAssumptions()
        _pin_outside(variables, incumbent, neighborhood.session_ids)
        if target is not None:
            variables.model.Minimize(tier_expressions[target])
        raw = solver.Solve(variables.model)
        variables.model.ClearAssumptions()
        repair_seconds = time.monotonic() - repair_started

        feasible = raw in (cp_model.OPTIMAL, cp_model.FEASIBLE)
        candidate_score = score_before
        accepted = False

        if feasible:
            candidate = _placement_from(variables, solver)
            candidate_score = evaluate(
                instance, candidate, priorities=variables.priorities
            )
            if candidate_score < score_before:
                incumbent = candidate
                best_score = candidate_score
                accepted = True

        records.append(
            IterationRecord(
                iteration=iteration,
                policy=policy.code,
                neighborhood_size=neighborhood.size,
                score_before=score_before.values,
                score_after=candidate_score.values,
                repair_seconds=repair_seconds,
                feasible=feasible,
                accepted=accepted,
                solver_status=solver.StatusName(raw),
                rationale=neighborhood.rationale,
            )
        )

        stalled = 0 if accepted else stalled + 1
        if stalled >= config.stall_limit:
            stopped_early = True
            break

    report = validate(
        instance,
        tuple(
            Assignment(p.session_id, p.start_slot_id, p.room_id)
            for p in incumbent.values()
        ),
    )

    return LnsOutcome(
        placement=incumbent,
        score=best_score,
        initial_score=initial_score,
        iterations=tuple(records),
        wall_seconds=time.monotonic() - started,
        validation_ok=report.is_valid,
        stopped_early=stopped_early,
    )
