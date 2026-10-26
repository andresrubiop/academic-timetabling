"""Executing one optimization run, with progress and cooperative cancellation.

This is where the eight phases the playbook names actually happen. Each one is
written to the run row before the work it describes begins, so a coordinator
watching sees `BUILDING_MODEL` while the model is being built rather than
after -- a progress display that updates on completion is a progress display
that shows the previous step.

Cancellation
============

CP-SAT is asked to stop, never killed. `_CancelCallback` is a solution
callback that polls the run row and calls `StopSearch()`, so the solve unwinds
normally and the best solution found so far survives. Killing the process
would throw that away and leave the row claiming OPTIMIZING with nobody
working on it -- which is the state the heartbeat reaper exists to clean up,
so it would be manufacturing work for the recovery path instead of doing the
job.

The poll is deliberately cheap and deliberately not every callback: a solution
callback can fire thousands of times a second on an easy instance, and a
database round trip inside it would dominate the solve.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
import traceback
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from academic_ai.application.optimization import PHASE_PROGRESS
from academic_ai.domain.enums import OptimizationRunStatus, SolverStatus
from academic_ai.infrastructure.db.models import OptimizationRun

__all__ = ["RunOutcome", "execute_run"]

log = logging.getLogger("academic_ai.runner")

#: Seconds between cancellation polls inside the solve.
_CANCEL_POLL_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: str
    status: OptimizationRunStatus
    solver_status: SolverStatus | None
    detail: str


def _advance(
    session: Session,
    run: OptimizationRun,
    phase: OptimizationRunStatus,
    message: str,
    *,
    worker_id: str,
    now: dt.datetime | None = None,
) -> None:
    """Record a phase and heartbeat in one commit.

    Committed immediately rather than at the end of the run: the whole point
    is that somebody else can see it. A progress update inside an uncommitted
    transaction is invisible to the API process reading the row, which is
    every reader.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    run.status = phase
    run.progress_percent = PHASE_PROGRESS[phase]
    run.progress_message = message
    run.worker_id = worker_id
    run.heartbeat_at = now
    session.commit()
    # `detail`, not `message`: `message` is a reserved LogRecord attribute and
    # passing it through `extra` raises KeyError inside the logging call --
    # which fails the run for a logging reason.
    log.info(
        "phase",
        extra={"run_id": str(run.id), "phase": phase.value, "detail": message},
    )


def _cancel_requested(session: Session, run_id: uuid.UUID) -> bool:
    """Re-read only the flag.

    `session.expire` on the whole object would reload every column including
    the JSONB blobs, on a timer, for the length of a solve.
    """
    from sqlalchemy import select

    session.commit()  # end the current snapshot, or the flag is never seen
    return (
        session.scalar(
            select(OptimizationRun.cancel_requested_at).where(
                OptimizationRun.id == run_id
            )
        )
        is not None
    )


def execute_run(
    session: Session,
    run_id: uuid.UUID,
    *,
    worker_id: str,
    heartbeat=None,
) -> RunOutcome:
    """Run one optimization to a terminal state. Never raises for a solver
    failure -- a failed run is a recorded outcome, not an exception that takes
    the worker down with it.

    ``heartbeat`` is called periodically so the caller can renew its queue
    lease, and once more with ``force=True`` immediately before results are
    written. It returns False when the lease was lost, which means another
    worker now owns this run and this one must stop *without* writing results:
    two workers writing results for one run is what the lease exists to
    prevent.
    """
    run = session.get(OptimizationRun, run_id)
    if run is None:
        return RunOutcome(str(run_id), OptimizationRunStatus.FAILED, None, "no such run")

    now = dt.datetime.now(dt.timezone.utc)

    # Cancelled while queued: nothing to do, and claiming it would restart a
    # run somebody already stopped.
    if run.cancel_requested_at is not None:
        _advance(session, run, OptimizationRunStatus.CANCELLED,
                 "cancelled before execution", worker_id=worker_id)
        run.finished_at = now
        session.commit()
        return RunOutcome(str(run_id), OptimizationRunStatus.CANCELLED, None, "cancelled")

    run.attempts += 1
    run.started_at = run.started_at or now
    session.commit()

    try:
        return _execute(session, run, worker_id=worker_id, heartbeat=heartbeat)
    except Exception as exc:  # noqa: BLE001
        # The failure is recorded on the row and the worker survives. A worker
        # that dies on a bad instance takes every queued run behind it down
        # too.
        log.exception("run failed", extra={"run_id": str(run_id)})
        session.rollback()
        run = session.get(OptimizationRun, run_id)
        run.status = OptimizationRunStatus.FAILED
        # UNKNOWN, not INFEASIBLE. The solver did not conclude anything -- the
        # run crashed -- and reporting INFEASIBLE would tell a coordinator that
        # no timetable exists when what happened is that our code broke. That
        # distinction is the one SESSION 3 exists to protect.
        run.solver_status = SolverStatus.UNKNOWN
        run.progress_percent = 100
        run.progress_message = "failed"
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        # The type and message, not the traceback: the row is read by a
        # coordinator, and file paths tell them nothing. The traceback is in
        # the log, correlated by run id.
        run.failure_reason = f"{type(exc).__name__}: {exc}"[:4000]
        session.commit()
        log.debug("traceback", extra={"run_id": str(run_id), "tb": traceback.format_exc()})
        return RunOutcome(
            str(run_id), OptimizationRunStatus.FAILED, SolverStatus.UNKNOWN, str(exc)
        )


def _execute(
    session: Session,
    run: OptimizationRun,
    *,
    worker_id: str,
    heartbeat,
) -> RunOutcome:
    from academic_ai.domain.scheduling.domains import compute_domains
    from academic_ai.domain.scheduling.feasibility import analyse
    from academic_ai.optimization.solve import SolverConfig, solve

    run_id = run.id
    params = run.solver_parameters or {}
    time_limit = float(params.get("time_limit_seconds", 60.0))
    seed = int(params.get("random_seed", 0))

    # --- VALIDATING ---------------------------------------------------------
    _advance(session, run, OptimizationRunStatus.VALIDATING,
             "checking the instance before spending a search on it",
             worker_id=worker_id)

    instance = _load_instance(session, run)
    report = analyse(instance)
    if not report.is_solvable:
        # SESSION 6's whole point: a named impossibility beats a bare
        # INFEASIBLE, and it is available *before* the solver runs.
        run.status = OptimizationRunStatus.FAILED
        run.solver_status = SolverStatus.INFEASIBLE
        run.progress_percent = 100
        run.progress_message = "the instance cannot be solved as stated"
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        run.failure_reason = "; ".join(f.message for f in report.errors)[:4000]
        session.commit()
        return RunOutcome(str(run_id), OptimizationRunStatus.FAILED,
                          SolverStatus.INFEASIBLE, "blocked before search")

    # --- BUILDING_MODEL -----------------------------------------------------
    _advance(session, run, OptimizationRunStatus.BUILDING_MODEL,
             "translating constraints into a CP-SAT model", worker_id=worker_id)
    _, diagnostics = compute_domains(instance.snapshot, instance.constraints)

    # --- SEARCHING_FEASIBLE -------------------------------------------------
    _advance(session, run, OptimizationRunStatus.SEARCHING_FEASIBLE,
             "searching for any schedule that satisfies every hard constraint",
             worker_id=worker_id)

    # `last_poll = 0` so the *first* callback checks rather than being skipped
    # by the rate limit. A solve that finishes inside one poll interval would
    # otherwise never look at the cancellation flag at all.
    state = {"cancelled": False, "lease_lost": False, "last_poll": 0.0}

    def _tick() -> bool:
        """Called from inside the solve. True means keep going."""
        if time.monotonic() - state["last_poll"] < _CANCEL_POLL_SECONDS:
            return True
        state["last_poll"] = time.monotonic()
        if heartbeat is not None and heartbeat() is False:
            state["lease_lost"] = True
            return False
        if _cancel_requested(session, run_id):
            state["cancelled"] = True
            return False
        run_row = session.get(OptimizationRun, run_id)
        if run_row is not None:
            run_row.heartbeat_at = dt.datetime.now(dt.timezone.utc)
            session.commit()
        return True

    outcome = solve(
        instance,
        SolverConfig(time_limit_seconds=time_limit, random_seed=seed),
        diagnostics=diagnostics,
        should_continue=_tick,
    )

    run = session.get(OptimizationRun, run_id)

    # Checked again *here*, not only inside the solve. The decisive moment is
    # the write, and a solve short enough that the callback never fired would
    # otherwise persist results under a lease this worker no longer holds --
    # which is the exact outcome the lease exists to prevent. Cheap: one Redis
    # round trip per run.
    if heartbeat is not None and heartbeat(force=True) is False:
        state["lease_lost"] = True

    if state["lease_lost"]:
        # Another worker owns this run. Write nothing.
        log.warning("stopping: lease lost", extra={"run_id": str(run_id)})
        return RunOutcome(str(run_id), run.status, None, "lease lost")

    if state["cancelled"]:
        run.status = OptimizationRunStatus.CANCELLED
        run.solver_status = outcome.solver_status
        run.progress_percent = 100
        run.progress_message = (
            "cancelled; the best schedule found before stopping was kept"
            if outcome.assignments
            else "cancelled before any schedule was found"
        )
        run.finished_at = dt.datetime.now(dt.timezone.utc)
        session.commit()
        return RunOutcome(str(run_id), OptimizationRunStatus.CANCELLED,
                          outcome.solver_status, "cancelled")

    # --- FEASIBLE_FOUND / OPTIMIZING / EVALUATING ---------------------------
    if outcome.assignments:
        _advance(session, run, OptimizationRunStatus.FEASIBLE_FOUND,
                 f"a schedule exists; {len(outcome.assignments)} sessions placed",
                 worker_id=worker_id)
        _advance(session, run, OptimizationRunStatus.OPTIMIZING,
                 "improving it tier by tier", worker_id=worker_id)

    _advance(session, run, OptimizationRunStatus.EVALUATING,
             "re-checking the result against every hard constraint independently",
             worker_id=worker_id)

    run = session.get(OptimizationRun, run_id)
    run.solver_status = outcome.solver_status
    run.finished_at = dt.datetime.now(dt.timezone.utc)
    run.progress_percent = 100

    if outcome.is_solved:
        run.status = OptimizationRunStatus.COMPLETED
        run.progress_message = "completed"
        if outcome.tiers:
            run.objective_value = float(outcome.tiers[0].value)
        _persist_metrics(session, run, outcome)
    else:
        run.status = OptimizationRunStatus.FAILED
        run.progress_message = "no schedule was produced"
        run.failure_reason = (
            outcome.failure_reason
            or f"solver returned {outcome.solver_status.value if outcome.solver_status else 'nothing'}"
        )[:4000]

    session.commit()
    return RunOutcome(str(run_id), run.status, run.solver_status, run.progress_message or "")


def _persist_metrics(session: Session, run: OptimizationRun, outcome) -> None:
    """One row per tier, so comparing runs is a query rather than parsing."""
    from academic_ai.infrastructure.db.models import OptimizationMetric

    for tier in outcome.tiers:
        session.add(
            OptimizationMetric(
                optimization_run_id=run.id,
                name=f"tier.{tier.code}",
                value=float(tier.value),
                unit="weighted_incidences",
            )
        )
    session.flush()


def _load_instance(session: Session, run: OptimizationRun):
    """Build the `ProblemInstance` this run should solve, from the database.

    SESSION 18 replaced the fixture that stood here for nine sessions. The
    same loader serves the schedule preview endpoints, deliberately: a preview
    computed against a different instance from the one the solver would use is
    worse than no preview, because it is confidently wrong rather than absent.

    ``MINIMAL_DISRUPTION`` when a base version exists and ``GREENFIELD`` when
    none does -- and that is not a preference. The domain refuses GREENFIELD
    for a snapshot carrying base assignments, because that profile omits
    `SCHEDULE_STABILITY` and would silently ignore them.
    """
    from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
    from academic_ai.domain.scheduling.problem import ProblemInstance
    from academic_ai.infrastructure.repositories import (
        load_constraints,
        load_snapshot,
    )

    snapshot, diagnostics = load_snapshot(
        session,
        run.academic_period_id,
        base_schedule_version_id=run.base_schedule_version_id,
    )
    constraints = load_constraints(session, run.academic_period_id)
    profile = "MINIMAL_DISRUPTION" if snapshot.base_assignments else "GREENFIELD"

    return ProblemInstance(
        snapshot=snapshot,
        constraints=constraints,
        objective_profile=OBJECTIVE_PROFILES[profile],
        diagnostics=diagnostics,
        scenario_id=str(run.scenario_id) if run.scenario_id else None,
    )
