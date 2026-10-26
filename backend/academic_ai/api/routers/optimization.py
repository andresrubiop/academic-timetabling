"""Submitting, watching and cancelling runs.

Nothing here does any optimization. `POST /runs` writes a row, pushes an id
and returns — the HTTP request that starts a twelve-minute solve finishes in
milliseconds. That is the playbook's "optimization must not block HTTP", and
it is why every other endpoint here is about *observing* a run rather than
performing one.

The SSE stream (ADR-0011) is a view over the database row, never the source of
truth. Any API replica can serve the stream for a run executing on any worker,
and a client that never opens it loses nothing but latency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from academic_ai.api.deps import get_optimization_service, require
from academic_ai.api.schemas import (
    CancelResponse,
    RunListResponse,
    RunResponse,
    SubmitRunRequest,
    SubmitRunResponse,
)
from academic_ai.application.optimization import (
    TERMINAL_STATUSES,
    OptimizationService,
    RunNotFound,
)
from academic_ai.security.rbac import Permission, Principal

router = APIRouter(prefix="/optimization", tags=["optimization"])
log = logging.getLogger("academic_ai.api.optimization")

#: How often the stream re-reads the row. Two seconds is well under the pace
#: at which phases change and far above the cost of the query.
_POLL_SECONDS = 2.0

#: A stream that never ends is a slow memory leak with a sympathetic
#: explanation. Terminates after this even if the run has not; the client
#: reconnects, and `EventSource` does that by itself.
_MAX_STREAM_SECONDS = 30 * 60


def _to_response(run) -> RunResponse:
    return RunResponse(
        run_id=str(run.id),
        status=run.status.value,
        progress_percent=run.progress_percent,
        progress_message=run.progress_message,
        solver_status=run.solver_status.value if run.solver_status else None,
        objective_value=float(run.objective_value) if run.objective_value is not None else None,
        failure_reason=run.failure_reason,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        heartbeat_at=run.heartbeat_at,
        attempts=run.attempts,
        cancel_requested=run.cancel_requested_at is not None,
        is_terminal=run.status in TERMINAL_STATUSES,
    )


@router.post(
    "/runs", response_model=SubmitRunResponse, status_code=status.HTTP_202_ACCEPTED
)
def submit_run(
    payload: SubmitRunRequest,
    principal: Principal = Depends(require(Permission.RUN_OPTIMIZATION)),
    service: OptimizationService = Depends(get_optimization_service),
) -> SubmitRunResponse:
    """202, not 201. The run is *accepted*; it has not happened yet, and a 201
    would tell the client a result exists."""
    try:
        submission = service.submit(
            principal,
            academic_period_id=uuid.UUID(payload.academic_period_id),
            base_schedule_version_id=(
                uuid.UUID(payload.base_schedule_version_id)
                if payload.base_schedule_version_id
                else None
            ),
            scenario_id=uuid.UUID(payload.scenario_id) if payload.scenario_id else None,
            time_limit_seconds=payload.time_limit_seconds,
            random_seed=payload.random_seed,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)
        ) from exc
    return SubmitRunResponse(
        run_id=submission.run_id,
        status=submission.status.value,
        queue_position=submission.queue_position,
    )


@router.get("/runs", response_model=RunListResponse)
def list_runs(
    limit: int = 50,
    principal: Principal = Depends(require(Permission.RUN_OPTIMIZATION)),
    service: OptimizationService = Depends(get_optimization_service),
) -> RunListResponse:
    runs = service.list_recent(principal, limit=limit)
    return RunListResponse(runs=[_to_response(r) for r in runs])


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(
    run_id: uuid.UUID,
    principal: Principal = Depends(require(Permission.RUN_OPTIMIZATION)),
    service: OptimizationService = Depends(get_optimization_service),
) -> RunResponse:
    try:
        return _to_response(service.get(principal, run_id))
    except RunNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None


@router.post("/runs/{run_id}/cancel", response_model=CancelResponse)
def cancel_run(
    run_id: uuid.UUID,
    principal: Principal = Depends(require(Permission.RUN_OPTIMIZATION)),
    service: OptimizationService = Depends(get_optimization_service),
) -> CancelResponse:
    """Ask a run to stop. Cooperative — see `OptimizationService.request_cancel`.

    200 rather than 202 even though the stopping is asynchronous, because the
    *request* is recorded synchronously and that is what the response reports.
    Cancelling an already-finished run is a success, not a conflict: two
    coordinators pressing cancel should both be told it is cancelled.
    """
    try:
        run = service.request_cancel(principal, run_id)
    except RunNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

    if run.status in TERMINAL_STATUSES and run.cancel_requested_at is None:
        detail = f"run had already finished as {run.status.value}"
    elif run.status.value == "CANCELLED":
        detail = "cancelled"
    else:
        detail = (
            "cancellation requested; the worker will stop at its next "
            "checkpoint and keep the best solution found so far"
        )
    return CancelResponse(
        run_id=str(run.id),
        status=run.status.value,
        cancel_requested=run.cancel_requested_at is not None,
        detail=detail,
    )


@router.get("/runs/{run_id}/events")
async def stream_run(
    run_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Permission.RUN_OPTIMIZATION)),
    service: OptimizationService = Depends(get_optimization_service),
) -> StreamingResponse:
    """Server-Sent Events for one run's progress (ADR-0011).

    Emits the current state immediately, then on every change, then closes on
    a terminal status. Closing rather than idling is what keeps a held
    connection from becoming a leak on a shared host.
    """
    try:
        service.get(principal, run_id)
    except RunNotFound:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND) from None

    async def events():
        last_payload: str | None = None
        waited = 0.0
        while waited < _MAX_STREAM_SECONDS:
            # A client that closes the tab must not leave this loop querying
            # the database every two seconds forever.
            if await request.is_disconnected():
                return
            try:
                run = service.get(principal, run_id)
            except RunNotFound:
                yield 'event: error\ndata: {"detail":"run disappeared"}\n\n'
                return

            payload = _to_response(run).model_dump_json()
            if payload != last_payload:
                # `id:` so a reconnecting EventSource can send Last-Event-ID.
                # Progress is idempotent, so replay costs nothing.
                yield f"id: {run.progress_percent}\nevent: progress\ndata: {payload}\n\n"
                last_payload = payload

            if run.status in TERMINAL_STATUSES:
                yield f"event: done\ndata: {payload}\n\n"
                return

            await asyncio.sleep(_POLL_SECONDS)
            waited += _POLL_SECONDS

        yield (
            "event: timeout\ndata: "
            + json.dumps({"detail": "stream closed; reconnect to continue watching"})
            + "\n\n"
        )

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx buffers proxied responses by default, which would hold
            # every event until the buffer filled and make the stream useless.
            "X-Accel-Buffering": "no",
        },
    )
