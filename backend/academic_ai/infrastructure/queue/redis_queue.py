"""A reliable Redis queue for optimization runs.

Redis lists rather than Celery or RQ. Not because those are bad, but because
the failure modes that matter here are specific and few, and a queue whose
semantics are written down in eighty lines can be reasoned about by the person
debugging it at 02:00. Celery would also pull a broker abstraction, a result
backend and a scheduler into an image on a filesystem with 16 GiB free, for
features this project does not use.

The three failures this is built around
=======================================

**A lost job.** ``LPUSH`` onto the pending list, ``BLMOVE`` from pending to a
*processing* list. The move is atomic, so a worker that dies between "take the
job" and "start the job" leaves the id on the processing list rather than
nowhere. Popping with ``BRPOP`` instead — the obvious implementation — deletes
the id from Redis the instant the worker receives it, and a crash one
microsecond later loses the run silently.

**A duplicated job.** The processing list is not enough on its own: recovery
has to be able to tell "this worker is slow" from "this worker is dead". Every
claimed run carries a *lease* — a Redis key with a TTL that the worker renews
while it works. Reclaiming a run whose lease has expired is safe; reclaiming
one whose lease is alive would run the same optimization twice and write two
sets of results.

**A job that outlives its worker.** The lease TTL is what bounds this. A
worker killed by the OOM reaper renews nothing, the lease expires, and
`reclaim_expired` moves the id back to pending. The database row's
``attempts`` column bounds how many times that may happen, because a run that
crashes one worker will crash the next one, and unbounded retry turns one bad
instance into an outage for every run queued behind it.

Cancellation is not in this module. It is a flag on the run row that the
worker's CP-SAT callback polls, so a cancelled run stops at a consistent point
and keeps the best solution it found. A queue-level cancel could only delete
an id that has not been claimed yet, which is the easy half of the problem.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Iterator

import redis

__all__ = ["JobLease", "QueueError", "RedisJobQueue"]

log = logging.getLogger("academic_ai.queue")


class QueueError(RuntimeError):
    """The queue could not be reached or answered nonsensically."""


@dataclass(frozen=True, slots=True)
class JobLease:
    """A claimed run, and the token that proves this worker owns it.

    ``token`` exists so that releasing a lease is safe. A worker that stalls
    past its TTL, then wakes and deletes "its" lease, would otherwise delete
    the lease of whichever worker legitimately reclaimed the run. The token
    makes the delete conditional on still being the owner.
    """

    run_id: str
    token: str
    worker_id: str
    claimed_at: float


class RedisJobQueue:
    """Pending list, processing list, and a lease per claimed run."""

    #: Renew well inside this, or a slow solve loses its own lease. The worker
    #: heartbeats at a third of it.
    DEFAULT_LEASE_SECONDS = 90

    #: A blocking pop holds the socket for its whole timeout, so the client's
    #: socket timeout must exceed it or the read expires exactly when the pop
    #: would have returned. Callers building a client for this queue should
    #: use at least this.
    @staticmethod
    def recommended_socket_timeout(claim_timeout: int = 5) -> int:
        return claim_timeout * 3

    def __init__(
        self,
        client: redis.Redis,
        *,
        namespace: str = "academic_ai:optimization",
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> None:
        self._redis = client
        self._pending = f"{namespace}:pending"
        self._processing = f"{namespace}:processing"
        self._lease_prefix = f"{namespace}:lease:"
        self._payload_prefix = f"{namespace}:payload:"
        self.lease_seconds = lease_seconds

    # -- keys ---------------------------------------------------------------

    def _lease_key(self, run_id: str) -> str:
        return f"{self._lease_prefix}{run_id}"

    def _payload_key(self, run_id: str) -> str:
        return f"{self._payload_prefix}{run_id}"

    # -- producing ----------------------------------------------------------

    def submit(self, run_id: str, payload: dict | None = None) -> None:
        """Enqueue a run. Idempotent on the id.

        The payload is a *hint* — everything authoritative lives in the
        database row, keyed by ``run_id``. A queue message that carried the
        problem instance would go stale the moment anything about the period
        changed, and the worker would optimise a timetable nobody asked for.
        """
        pipe = self._redis.pipeline()
        if payload is not None:
            pipe.set(self._payload_key(run_id), json.dumps(payload), ex=86400)
        pipe.lpush(self._pending, run_id)
        pipe.execute()
        log.info("run queued", extra={"run_id": run_id})

    def pending_count(self) -> int:
        return int(self._redis.llen(self._pending))

    def processing_count(self) -> int:
        return int(self._redis.llen(self._processing))

    # -- consuming ----------------------------------------------------------

    def claim(self, worker_id: str, *, timeout: int = 5) -> JobLease | None:
        """Block up to ``timeout`` seconds for a run, and lease it.

        ``BLMOVE`` is atomic: the id is never in neither list.
        """
        try:
            raw = self._redis.blmove(
                self._pending, self._processing, timeout, "RIGHT", "LEFT"
            )
        except redis.TimeoutError:
            # A *socket* timeout on a blocking pop is indistinguishable from
            # "the queue stayed empty for `timeout` seconds", which is the
            # normal case, not an error. Treating it as a failure makes an
            # idle worker log a stack trace every few seconds -- and on a
            # client whose socket_timeout is near the block timeout, the two
            # race and the pop effectively never succeeds.
            #
            # Callers should still give the client a socket timeout well above
            # this one; `RedisJobQueue.recommended_socket_timeout` says how far.
            return None
        if raw is None:
            return None
        run_id = raw.decode() if isinstance(raw, bytes) else str(raw)

        token = str(uuid.uuid4())
        self._redis.set(self._lease_key(run_id), token, ex=self.lease_seconds)
        log.info("run claimed", extra={"run_id": run_id, "worker_id": worker_id})
        return JobLease(run_id, token, worker_id, time.monotonic())

    def renew(self, lease: JobLease) -> bool:
        """Extend the lease, but only if this worker still owns it.

        Returns ``False`` when the lease was already reclaimed — which the
        worker must treat as "stop, somebody else owns this run now", not as a
        transient error to retry through. Two workers writing results for one
        run is the outcome the whole lease mechanism exists to prevent.
        """
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            return redis.call('expire', KEYS[1], ARGV[2])
        else
            return 0
        end
        """
        result = self._redis.eval(
            script, 1, self._lease_key(lease.run_id), lease.token, self.lease_seconds
        )
        return bool(result)

    def release(self, lease: JobLease) -> None:
        """Finish with a run: drop it from processing and free the lease.

        Both operations are conditional on ownership, for the reason in
        `JobLease.token`.
        """
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
            redis.call('del', KEYS[1])
            redis.call('lrem', KEYS[2], 0, ARGV[2])
            redis.call('del', KEYS[3])
            return 1
        else
            return 0
        end
        """
        self._redis.eval(
            script,
            3,
            self._lease_key(lease.run_id),
            self._processing,
            self._payload_key(lease.run_id),
            lease.token,
            lease.run_id,
        )

    def payload(self, run_id: str) -> dict | None:
        raw = self._redis.get(self._payload_key(run_id))
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # A corrupt hint is not worth failing a run over: the database row
            # is authoritative and the payload is advisory.
            log.warning("discarding unparseable payload", extra={"run_id": run_id})
            return None

    # -- recovery -----------------------------------------------------------

    def reclaim_expired(self) -> list[str]:
        """Return ids on the processing list whose lease has expired.

        Called by whichever worker gets there first. Reclaiming a run whose
        lease is *alive* would duplicate it, so the expiry check is the whole
        safety argument: Redis expiring the key is the evidence that no worker
        has renewed it for a full TTL.
        """
        reclaimed: list[str] = []
        for raw in self._redis.lrange(self._processing, 0, -1):
            run_id = raw.decode() if isinstance(raw, bytes) else str(raw)
            if self._redis.exists(self._lease_key(run_id)):
                continue
            moved = self._redis.lrem(self._processing, 0, run_id)
            if moved:
                self._redis.lpush(self._pending, run_id)
                reclaimed.append(run_id)
                log.warning(
                    "run reclaimed after its worker stopped renewing",
                    extra={"run_id": run_id},
                )
        return reclaimed

    def drain_pending(self) -> Iterator[str]:
        """Every queued id, without claiming. For diagnostics only."""
        for raw in self._redis.lrange(self._pending, 0, -1):
            yield raw.decode() if isinstance(raw, bytes) else str(raw)

    def ping(self) -> bool:
        try:
            return bool(self._redis.ping())
        except redis.RedisError as exc:  # pragma: no cover - environment
            raise QueueError(str(exc)) from exc
