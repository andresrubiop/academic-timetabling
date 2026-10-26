"""Optimizer worker. ``python -m academic_ai.worker``.

Claims runs from the queue, executes them, and renews its lease while it
works. One run at a time per process: CP-SAT is already parallel internally,
and two solves in one process compete for the same cores while making the
memory limit twice as easy to hit. Concurrency is more workers, not more
threads.

Startup verifies that this process can build a CP-SAT model before it claims
anything. A worker that cannot must fail at deploy time, loudly, rather than
at 02:00 when the first run is submitted -- `ortools` was imported but
undeclared from SESSION 3 to SESSION 8, which is precisely that failure
waiting to happen.
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable

import redis

from academic_ai.security.logging import configure_logging

if TYPE_CHECKING:  # pragma: no cover - import cycle at runtime, types only
    from academic_ai.infrastructure.queue import RedisJobQueue

log = logging.getLogger("academic_ai.worker")

#: How often to renew the lease and heartbeat. A third of the lease TTL, so
#: two consecutive misses are survivable.
_HEARTBEAT_SECONDS = 25

#: How often to look for runs abandoned by a dead worker.
_REAP_EVERY_SECONDS = 60


def worker_concurrency(raw: str | None = None) -> int:
    """How many runs this process was *asked* to handle at once.

    Read here rather than inline so the answer to "what does a nonsense value
    do" is a test rather than a traceback in a container that will not start.
    Anything unparseable means 1, because refusing to boot over a malformed
    tuning knob is a worse outcome than ignoring it.
    """
    raw = os.environ.get("OPTIMIZER_WORKER_CONCURRENCY", "1") if raw is None else raw
    try:
        return max(1, int((raw or "1").strip()))
    except ValueError:
        log.warning(
            "OPTIMIZER_WORKER_CONCURRENCY=%r is not a number; using 1", raw
        )
        return 1


def _verify_optimizer() -> None:
    from ortools.sat.python import cp_model

    from academic_ai.domain.scheduling.fixtures import FIXTURES
    from academic_ai.optimization.variables import build_variables

    # Building a model proves the native OR-Tools extension loaded, which is
    # the part that breaks on a mismatched base image. Importing only proves
    # the Python package is present.
    instance = FIXTURES["TRIVIAL_FEASIBLE"].instance()
    variables = build_variables(instance)
    assert isinstance(variables.model, cp_model.CpModel)
    log.info("optimizer verified", extra={"ortools": True})


def reap_once(queue: "RedisJobQueue", make_session: Callable[[], Any]) -> None:
    """One recovery pass: requeue runs whose worker died.

    Never raises. Recovery failing must not stop the worker doing its actual
    job -- a Redis blip during a reaper pass would otherwise take the process
    down and turn one abandoned run into a stopped queue.
    """
    from academic_ai.application.optimization import OptimizationService

    try:
        with make_session() as session:
            reaped = OptimizationService(session, queue).reap_abandoned()
            session.commit()
            if reaped:
                log.warning("reaped abandoned runs", extra={"count": len(reaped)})
        queue.reclaim_expired()
    except Exception:  # noqa: BLE001
        log.exception("reaper pass failed")


def run_lease(
    lease: Any,
    *,
    queue: "RedisJobQueue",
    make_session: Callable[[], Any],
    worker_id: str,
    execute: Callable[..., Any] | None = None,
) -> None:
    """Execute one claimed run, and **always** release the lease.

    Extracted from the loop so the guarantee in that sentence is assertable.
    Leaving an id on the processing list would make the reaper requeue a run
    that actually finished -- which is worse than losing one, because the
    second execution writes a second set of results for the same request.
    """
    if execute is None:
        from academic_ai.application.runner import execute_run as execute

    log.info("run claimed", extra={"run_id": lease.run_id, "worker_id": worker_id})
    last_renew = time.monotonic()

    def _heartbeat(force: bool = False) -> bool:
        """Renew the lease. False means another worker owns this run now.

        ``force`` skips the rate limit. The runner uses it immediately before
        writing results, where the answer decides whether the write happens at
        all -- a fast solve whose callback never fired would otherwise persist
        under a lease it had already lost.
        """
        nonlocal last_renew
        if not force and time.monotonic() - last_renew < _HEARTBEAT_SECONDS:
            return True
        last_renew = time.monotonic()
        alive = queue.renew(lease)
        if not alive:
            log.warning("lease lost", extra={"run_id": lease.run_id})
        return alive

    try:
        with make_session() as session:
            outcome = execute(
                session,
                uuid.UUID(lease.run_id),
                worker_id=worker_id,
                heartbeat=_heartbeat,
            )
            session.commit()
        log.info(
            "run finished",
            extra={"run_id": lease.run_id, "status": outcome.status.value},
        )
    except Exception:  # noqa: BLE001
        log.exception("run raised out of execute_run", extra={"run_id": lease.run_id})
    finally:
        try:
            queue.release(lease)
        except redis.RedisError:
            log.exception("failed to release lease", extra={"run_id": lease.run_id})


def main() -> int:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))

    from academic_ai.security.settings import ConfigurationError, load_security_settings

    try:
        settings = load_security_settings()
    except ConfigurationError as exc:
        log.error("worker refusing to start: %s", exc)
        return 2

    worker_id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    log.info(
        "worker starting",
        extra={"worker_id": worker_id, "settings": settings.redacted()},
    )

    try:
        _verify_optimizer()
    except Exception:
        log.exception("worker cannot build a CP-SAT model; refusing to start")
        return 3

    if not settings.redis_url:
        log.error("REDIS_URL is not set; the worker has no queue to consume")
        return 4

    from academic_ai.infrastructure.db.session import session_factory
    from academic_ai.infrastructure.queue import RedisJobQueue

    # socket_timeout comfortably above the blocking claim, or the read expires
    # exactly when the pop would have returned and the worker never gets a job.
    client = redis.Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=5,
        socket_timeout=RedisJobQueue.recommended_socket_timeout(5),
        health_check_interval=30,
    )
    queue = RedisJobQueue(client)
    make_session = session_factory()

    stopping = threading.Event()

    def _stop(signum: int, _frame: object) -> None:
        # SIGTERM on `docker compose down`. Setting a flag rather than exiting
        # lets the run in flight finish its current phase and commit, so a
        # deploy does not manufacture abandoned runs for the reaper.
        log.info("worker stopping after the current run", extra={"signal": signum})
        stopping.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    last_reap = 0.0
    # A knob that does nothing is worse than no knob: it makes capacity
    # planning a fiction. This process runs one solve at a time on purpose --
    # CP-SAT is already parallel internally, and two solves in one process
    # compete for the same cores while doubling the memory ceiling. Say so,
    # rather than letting an operator believe setting it to 8 changed
    # anything.
    requested = worker_concurrency()
    if requested > 1:
        log.warning(
            "OPTIMIZER_WORKER_CONCURRENCY is %s, but one worker process runs "
            "one run at a time. Concurrency comes from more worker containers: "
            "`docker compose up -d --scale worker=%s`.",
            requested,
            requested,
        )

    log.info("worker ready", extra={"worker_id": worker_id})

    while not stopping.is_set():
        now = time.monotonic()
        if now - last_reap > _REAP_EVERY_SECONDS:
            last_reap = now
            reap_once(queue, make_session)

        try:
            lease = queue.claim(worker_id, timeout=5)
        except redis.RedisError:
            log.exception("queue unreachable; retrying")
            time.sleep(5)
            continue

        if lease is None:
            continue

        run_lease(
            lease, queue=queue, make_session=make_session, worker_id=worker_id
        )

    log.info("worker stopped", extra={"worker_id": worker_id})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
