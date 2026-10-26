"""Liveness and readiness.

Two endpoints, because they answer different questions and conflating them
causes an outage. `/healthz` says the process is alive; if it required the
database, a brief Postgres restart would make every replica unhealthy and an
orchestrator would kill them all. `/readyz` says the process can actually
serve, and is what a load balancer should drain on.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi import Depends

from academic_ai.api.deps import get_queue, get_session
from academic_ai.api.schemas import HealthResponse
from academic_ai.infrastructure.queue import RedisJobQueue

router = APIRouter(tags=["health"])


@router.get("/healthz", response_model=HealthResponse)
def healthz() -> HealthResponse:
    """Alive. Deliberately checks nothing external."""
    return HealthResponse(status="ok")


@router.get("/readyz", response_model=HealthResponse)
def readyz(
    response: Response,
    session: Session = Depends(get_session),
    queue: RedisJobQueue = Depends(get_queue),
) -> HealthResponse:
    checks: dict[str, dict[str, object]] = {}

    try:
        session.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        checks["database"] = {"ok": False, "detail": type(exc).__name__}

    try:
        queue.ping()
        checks["queue"] = {"ok": True, "pending": queue.pending_count()}
    except Exception as exc:  # noqa: BLE001
        checks["queue"] = {"ok": False, "detail": type(exc).__name__}

    healthy = all(c.get("ok") for c in checks.values())
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(status="ok" if healthy else "unhealthy", checks=checks)
