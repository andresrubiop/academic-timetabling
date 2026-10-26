"""Solve orchestration.

Two things here matter more than the search itself.

**A blocked instance never reaches the solver.** If the builder produced a
blocking diagnostic — a session with no feasible start, an unapproved merge —
the run stops and reports that diagnostic. Handing it to CP-SAT would return
INFEASIBLE, which is true, useless, and indistinguishable from a genuinely
over-constrained timetable. "No solution exists" and "you asked for a room
with fume hoods and there are none" are different answers and only one tells
anybody what to do.

**Progress and outcome are separate.** `OptimizationRunStatus` is how far the
run got; `SolverStatus` is what the solver concluded. INFEASIBLE (proven: no
schedule exists) is a completely different fact from UNKNOWN or TIMEOUT (the
search gave up), and conflating them is how a coordinator gets told their
timetable is impossible because a 60-second budget expired.
"""

from __future__ import annotations

import logging

import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from ortools.sat.python import cp_model

from academic_ai.domain.enums import OptimizationRunStatus, SolverStatus
from academic_ai.domain.scheduling.diagnostics import Diagnostic
from academic_ai.domain.scheduling.priority import ConflictPriorityProvider
from academic_ai.domain.scheduling.problem import ProblemInstance, missing_adapters
from academic_ai.optimization.adapters import (
    AdapterResult,
    apply_hard_constraints,
    registered_codes,
)
from academic_ai.optimization.objective import TierOutcome, optimise_lexicographically
from academic_ai.optimization.validator import (
    Assignment,
    ValidationReport,
    validate,
)
from academic_ai.optimization.variables import ModelVariables, build_variables

__all__ = ["SolveOutcome", "SolverConfig", "solve"]

#: CP-SAT status -> our vocabulary. The distinction TIMEOUT/UNKNOWN is ours,
#: not CP-SAT's: it reports UNKNOWN for both, so the caller has to know
#: whether the budget ran out.
_STATUS = {
    cp_model.OPTIMAL: SolverStatus.OPTIMAL,
    cp_model.FEASIBLE: SolverStatus.FEASIBLE,
    cp_model.INFEASIBLE: SolverStatus.INFEASIBLE,
    cp_model.MODEL_INVALID: SolverStatus.UNKNOWN,
    cp_model.UNKNOWN: SolverStatus.UNKNOWN,
}


@dataclass(frozen=True, slots=True)
class SolverConfig:
    """Deterministic by default.

    ``random_seed`` and single-threaded search are not performance choices:
    SESSION 3 requires reproducible fixtures and SESSION 4 forbids claiming an
    LNS improvement without reproducible evidence. Multi-threaded CP-SAT is
    nondeterministic, so a benchmark run with it cannot be replayed. Callers
    that want throughput set ``workers`` explicitly and accept the trade.
    """

    time_limit_seconds: float = 60.0
    workers: int = 1
    random_seed: int = 0
    log_search: bool = False
    #: Let CP-SAT repair a hint that is partial or slightly infeasible instead
    #: of discarding it. Re-optimisation always hints an existing timetable,
    #: and that hint is *never* complete — the whole reason to re-optimise is
    #: that something about it no longer works. Without repair, the hint is
    #: dropped and the solver starts from nothing: on the real 399-activity
    #: import that is the difference between 1.4 seconds and a 60-second
    #: UNKNOWN.
    repair_hint: bool = True

    def apply_to(self, solver: cp_model.CpSolver) -> None:
        solver.parameters.max_time_in_seconds = self.time_limit_seconds
        solver.parameters.num_workers = self.workers
        solver.parameters.random_seed = self.random_seed
        solver.parameters.log_search_progress = self.log_search
        solver.parameters.repair_hint = self.repair_hint


@dataclass(frozen=True, slots=True)
class SolveOutcome:
    status: OptimizationRunStatus
    solver_status: SolverStatus | None
    assignments: tuple[Assignment, ...] = ()
    validation: ValidationReport | None = None
    diagnostics: tuple[Diagnostic, ...] = ()
    tiers: tuple[TierOutcome, ...] = ()
    adapters: tuple[AdapterResult, ...] = ()
    wall_seconds: float = 0.0
    failure_reason: str | None = None

    @property
    def is_solved(self) -> bool:
        """A schedule the validator rejected is not a solved run.

        `solver_status` stays OPTIMAL — that is what CP-SAT honestly concluded
        about its own model — but the run did not succeed, and a caller that
        trusted this to publish would publish a double-booked timetable. The
        two vocabularies are separate precisely so this can be said.
        """
        if self.validation is not None and not self.validation.is_valid:
            return False
        return self.solver_status in (SolverStatus.OPTIMAL, SolverStatus.FEASIBLE)

    @property
    def is_proven_infeasible(self) -> bool:
        """True only when the solver *proved* it. Never true on a timeout."""
        return self.solver_status is SolverStatus.INFEASIBLE

    def metrics(self) -> Mapping[str, float]:
        out: dict[str, float] = {"wall_seconds": self.wall_seconds}
        for tier in self.tiers:
            out[f"tier.{tier.code}.value"] = float(tier.value)
        out["assignments"] = float(len(self.assignments))
        out["violations"] = float(
            len(self.validation.violations) if self.validation else 0
        )
        return out


def _extract(v: ModelVariables, solver: cp_model.CpSolver) -> tuple[Assignment, ...]:
    out: list[Assignment] = []
    for session_id, lits in v.start_lit.items():
        slot_id = next(
            (sid for sid, lit in lits.items() if solver.BooleanValue(lit)), None
        )
        if slot_id is None:  # pragma: no cover - ExactlyOne guarantees one
            continue
        room_id = None
        for rid, lit in v.room_lit.get(session_id, {}).items():
            if solver.BooleanValue(lit):
                room_id = rid
                break
        out.append(Assignment(session_id, slot_id, room_id))
    return tuple(sorted(out, key=lambda a: a.session_id))


class _Stopper(cp_model.CpSolverSolutionCallback):
    """Polls a predicate and asks CP-SAT to stop when it says so.

    A solution callback rather than a timer thread, because CP-SAT only offers
    an in-search hook here and a thread calling `StopSearch()` from outside is
    not supported. The consequence is worth knowing: on an instance where the
    solver finds no solution at all, this never fires, and cancellation waits
    for the time limit. That is acceptable -- the time limit is bounded and
    the alternative is killing a process mid-write.

    The predicate is expected to rate-limit itself; this class does not, since
    it cannot know what the predicate costs.
    """

    def __init__(self, predicate) -> None:
        super().__init__()
        self._predicate = predicate
        self.stopped = False

    def on_solution_callback(self) -> None:  # pragma: no cover - solver-driven
        try:
            if self._predicate() is False:
                self.stopped = True
                self.StopSearch()
        except Exception:  # noqa: BLE001
            # A failing predicate must not take the solve down with it. The
            # search continues to its time limit, which is the safe direction.
            logging.getLogger("academic_ai.optimization").exception(
                "cancellation predicate raised; continuing the search"
            )


def solve(
    instance: ProblemInstance,
    config: SolverConfig | None = None,
    *,
    diagnostics: Sequence[Diagnostic] = (),
    priority_provider: ConflictPriorityProvider | None = None,
    should_continue: Callable[[], bool] | None = None,
) -> SolveOutcome:
    """Solve one instance, or explain why it was never solvable.

    ``priority_provider`` ranks students within a conflict category. ``None``
    is the normal case and means the heuristic: no AI model is ever selected
    here, because no model activates automatically (CLAUDE.md — Human
    authority). A caller that wants one passes it explicitly, having read it
    from the registry, and the code that produced the ranking is recorded on
    the outcome.
    """
    config = config or SolverConfig()
    started = time.monotonic()
    all_diagnostics = tuple(instance.diagnostics) + tuple(diagnostics)

    # --- refuse to solve what should not be solved --------------------------
    blocking = tuple(d for d in all_diagnostics if d.is_blocking)
    if blocking:
        return SolveOutcome(
            status=OptimizationRunStatus.FAILED,
            solver_status=None,
            diagnostics=all_diagnostics,
            wall_seconds=time.monotonic() - started,
            failure_reason=(
                "instance is blocked before search: "
                + "; ".join(d.message for d in blocking[:3])
            ),
        )

    unimplemented = missing_adapters(instance, sorted(registered_codes()))
    if unimplemented:
        return SolveOutcome(
            status=OptimizationRunStatus.FAILED,
            solver_status=None,
            diagnostics=all_diagnostics,
            wall_seconds=time.monotonic() - started,
            failure_reason=(
                "no adapter for HARD constraints "
                f"{sorted(unimplemented)}; refusing to solve, because an "
                "unenforced HARD constraint returns a confident OPTIMAL for "
                "an impossible schedule"
            ),
        )

    # --- build ---------------------------------------------------------------
    variables = build_variables(instance, priority_provider=priority_provider)
    adapters = apply_hard_constraints(variables)

    # --- feasibility ---------------------------------------------------------
    solver = cp_model.CpSolver()
    # A solution callback is the only supported way to stop CP-SAT from
    # outside. `StopSearch()` is cooperative: it sets a flag the workers check
    # between nodes, so the search ends at a consistent point rather than in
    # the middle of propagating.
    stopper = _Stopper(should_continue) if should_continue is not None else None
    config.apply_to(solver)
    raw = solver.Solve(variables.model, stopper) if stopper else solver.Solve(variables.model)
    status = _STATUS.get(raw, SolverStatus.UNKNOWN)

    if status is SolverStatus.UNKNOWN and solver.WallTime() >= (
        config.time_limit_seconds * 0.95
    ):
        status = SolverStatus.TIMEOUT

    if status in (SolverStatus.INFEASIBLE, SolverStatus.UNKNOWN, SolverStatus.TIMEOUT):
        return SolveOutcome(
            status=(
                OptimizationRunStatus.COMPLETED
                if status is SolverStatus.INFEASIBLE
                else OptimizationRunStatus.FAILED
            ),
            solver_status=status,
            diagnostics=all_diagnostics,
            adapters=adapters,
            wall_seconds=time.monotonic() - started,
            failure_reason=(
                None
                if status is SolverStatus.INFEASIBLE
                else f"search did not conclude ({status.value})"
            ),
        )

    # --- optimise, tier by tier ---------------------------------------------
    tiers, final_status, assignments = optimise_lexicographically(
        variables, solver, config, lambda: _extract(variables, solver)
    )
    report = validate(instance, assignments)

    return SolveOutcome(
        status=(
            OptimizationRunStatus.COMPLETED
            if report.is_valid
            else OptimizationRunStatus.FAILED
        ),
        solver_status=final_status,
        assignments=assignments,
        validation=report,
        diagnostics=all_diagnostics,
        tiers=tiers,
        adapters=adapters,
        wall_seconds=time.monotonic() - started,
        failure_reason=(
            None
            if report.is_valid
            else f"schedule failed independent validation: {report.violations[0]}"
        ),
    )
