"""Submitting, watching and cancelling optimization runs.

**Optimization must not block HTTP.** `submit()` writes a row, pushes an id,
and returns. Everything expensive happens in the worker. The HTTP request that
starts a twelve-minute CP-SAT solve finishes in milliseconds, and the run is
then a resource with a URL like any other.

The database row is authoritative for everything. The queue carries an id and
an advisory hint; a queue message that carried the problem instance would go
stale the moment anything about the period changed, and the worker would
optimise a timetable nobody asked for.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.domain.enums import OptimizationRunStatus, SolverStatus
from academic_ai.infrastructure.db.models import OptimizationRun
from academic_ai.infrastructure.queue import RedisJobQueue
from academic_ai.security.rbac import Permission, Principal, authorise

__all__ = [
    "MAX_ATTEMPTS",
    "PHASE_PROGRESS",
    "TERMINAL_STATUSES",
    "OptimizationService",
    "RunNotFound",
    "RunSubmission",
]

log = logging.getLogger("academic_ai.optimization")

#: Phase -> percentage. Coarse and honest. CP-SAT cannot say how close it is
#: to proving optimality, so a finer number would be invented, and a UI would
#: render the invention as a smooth bar. The jump from SEARCHING_FEASIBLE to
#: FEASIBLE_FOUND is large because it is the moment the answer changes from
#: "maybe nothing exists" to "something does".
PHASE_PROGRESS: Mapping[OptimizationRunStatus, int] = {
    OptimizationRunStatus.QUEUED: 0,
    OptimizationRunStatus.VALIDATING: 5,
    OptimizationRunStatus.BUILDING_MODEL: 15,
    OptimizationRunStatus.SEARCHING_FEASIBLE: 30,
    OptimizationRunStatus.FEASIBLE_FOUND: 60,
    OptimizationRunStatus.OPTIMIZING: 75,
    OptimizationRunStatus.EVALUATING: 90,
    OptimizationRunStatus.COMPLETED: 100,
    OptimizationRunStatus.FAILED: 100,
    OptimizationRunStatus.CANCELLED: 100,
}

TERMINAL_STATUSES = frozenset(
    {
        OptimizationRunStatus.COMPLETED,
        OptimizationRunStatus.FAILED,
        OptimizationRunStatus.CANCELLED,
    }
)

#: Beyond this, stop handing the run to workers. A run that crashes one worker
#: will crash the next, and unbounded retry turns one bad instance into an
#: outage for every run queued behind it.
MAX_ATTEMPTS = 3


class RunNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class RunSubmission:
    run_id: str
    status: OptimizationRunStatus
    queue_position: int


class OptimizationService:
    def __init__(self, session: Session, queue: RedisJobQueue) -> None:
        self._session = session
        self._queue = queue

    # -- submitting ---------------------------------------------------------

    def submit(
        self,
        principal: Principal,
        *,
        academic_period_id: uuid.UUID,
        base_schedule_version_id: uuid.UUID | None = None,
        scenario_id: uuid.UUID | None = None,
        time_limit_seconds: float = 60.0,
        random_seed: int = 0,
        now: dt.datetime | None = None,
    ) -> RunSubmission:
        """Create a run and queue it. Returns immediately."""
        authorise(principal, Permission.RUN_OPTIMIZATION)
        now = now or dt.datetime.now(dt.timezone.utc)

        run = OptimizationRun(
            academic_period_id=academic_period_id,
            base_schedule_version_id=base_schedule_version_id,
            scenario_id=scenario_id,
            status=OptimizationRunStatus.QUEUED,
            progress_percent=PHASE_PROGRESS[OptimizationRunStatus.QUEUED],
            progress_message="queued",
            random_seed=random_seed,
            solver_parameters={
                "time_limit_seconds": time_limit_seconds,
                "random_seed": random_seed,
            },
            created_by_user_id=uuid.UUID(principal.user_id),
        )
        self._session.add(run)
        # Flush, not commit: the caller owns the transaction. But the row must
        # exist before the id is queued, or a worker fast enough to claim it
        # would look for a run that is not there yet.
        self._session.flush()

        run_id = str(run.id)
        self._queue.submit(run_id, {"time_limit_seconds": time_limit_seconds})
        log.info(
            "optimization submitted",
            extra={"run_id": run_id, "user_id": principal.user_id},
        )
        return RunSubmission(
            run_id=run_id,
            status=run.status,
            queue_position=self._queue.pending_count(),
        )

    # -- reading ------------------------------------------------------------

    def get(self, principal: Principal, run_id: uuid.UUID) -> OptimizationRun:
        authorise(principal, Permission.RUN_OPTIMIZATION)
        run = self._session.get(OptimizationRun, run_id)
        if run is None:
            raise RunNotFound(str(run_id))
        return run

    def list_recent(
        self, principal: Principal, *, limit: int = 50
    ) -> Sequence[OptimizationRun]:
        authorise(principal, Permission.RUN_OPTIMIZATION)
        return list(
            self._session.scalars(
                select(OptimizationRun)
                .order_by(OptimizationRun.created_at.desc())
                .limit(min(limit, 200))
            )
        )

    # -- cancelling ---------------------------------------------------------

    def request_cancel(
        self,
        principal: Principal,
        run_id: uuid.UUID,
        *,
        now: dt.datetime | None = None,
    ) -> OptimizationRun:
        """Ask a run to stop. **Cooperative**, and deliberately so.

        This sets a flag; the worker's CP-SAT callback polls it and calls
        `StopSearch()`. The run then stops at a consistent point and keeps the
        best solution it had found, which for a long solve is usually a
        perfectly usable timetable.

        Killing the worker instead would throw that away and leave the row
        claiming OPTIMIZING with nobody working on it -- which is exactly the
        state the heartbeat reaper exists to clean up, so it would be creating
        work for the recovery path rather than doing the job.

        Cancelling an already-terminal run is a no-op, not an error: two
        coordinators pressing cancel on the same run should both be told it is
        cancelled.
        """
        run = self.get(principal, run_id)
        now = now or dt.datetime.now(dt.timezone.utc)

        if run.status in TERMINAL_STATUSES:
            return run

        run.cancel_requested_at = now
        run.cancel_requested_by_user_id = uuid.UUID(principal.user_id)

        if run.status is OptimizationRunStatus.QUEUED:
            # Nobody has claimed it, so there is no cooperative party to wait
            # for. Terminating here is safe and immediate.
            run.status = OptimizationRunStatus.CANCELLED
            run.progress_percent = 100
            run.progress_message = "cancelled before it started"
            run.finished_at = now

        self._session.flush()
        log.info(
            "cancellation requested",
            extra={"run_id": str(run_id), "user_id": principal.user_id},
        )
        return run

    # -- recovery -----------------------------------------------------------

    def reap_abandoned(
        self, *, stale_after_seconds: int = 180, now: dt.datetime | None = None
    ) -> list[uuid.UUID]:
        """Fail or requeue runs whose worker stopped heartbeating.

        Without this a run whose worker was OOM-killed sits in OPTIMIZING
        forever, indistinguishable from one that is merely slow, and the
        coordinator watching it has no way to tell.

        Past `MAX_ATTEMPTS` the run is FAILED rather than requeued: a run that
        killed three workers is a bug report, not a retry.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(seconds=stale_after_seconds)
        active = [
            s
            for s in OptimizationRunStatus
            if s not in TERMINAL_STATUSES and s is not OptimizationRunStatus.QUEUED
        ]

        stale = self._session.scalars(
            select(OptimizationRun).where(
                OptimizationRun.status.in_(active),
                OptimizationRun.heartbeat_at.is_not(None),
                OptimizationRun.heartbeat_at < cutoff,
            )
        )
        reaped: list[uuid.UUID] = []
        for run in stale:
            if run.attempts >= MAX_ATTEMPTS:
                run.status = OptimizationRunStatus.FAILED
                # The schema requires a terminal run to say what the solver
                # concluded, and it is right to: a FAILED row with no verdict
                # cannot be told apart from one whose verdict was lost. Here
                # the solver concluded nothing -- the worker vanished --
                # which is exactly what UNKNOWN means. Never INFEASIBLE: that
                # would tell a coordinator no timetable exists when the truth
                # is that we stopped being able to look.
                run.solver_status = run.solver_status or SolverStatus.UNKNOWN
                run.finished_at = now
                run.progress_percent = 100
                run.failure_reason = (
                    f"abandoned by its worker {run.attempts} times; not retried "
                    "again. A run that repeatedly kills its worker is a bug "
                    "report, not something to keep retrying."
                )
                log.error("run abandoned permanently", extra={"run_id": str(run.id)})
            else:
                run.status = OptimizationRunStatus.QUEUED
                run.progress_percent = PHASE_PROGRESS[OptimizationRunStatus.QUEUED]
                run.progress_message = "requeued after its worker stopped responding"
                run.worker_id = None
                run.heartbeat_at = None
                self._queue.submit(str(run.id))
                log.warning("run requeued", extra={"run_id": str(run.id)})
            reaped.append(run.id)
        self._session.flush()
        return reaped
