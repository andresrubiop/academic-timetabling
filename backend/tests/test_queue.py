"""SESSION 9: the optimization job queue, and the three ways it could lose work.

Tested against a **real** Redis rather than a fake, because everything this
module claims rests on Redis semantics -- `BLMOVE` being atomic, `EVAL` being
atomic -- and a fake that implements those approximately would let the
approximation be what is verified.

Point `TEST_REDIS_URL` at a throwaway instance. The default matches the
loopback-bound `academic_ai_dev_redis` container SESSION 9 created for exactly
this; the stack's own Redis is deliberately unreachable from the host.
"""

from __future__ import annotations

import os
import time

import pytest

redis = pytest.importorskip("redis")

from academic_ai.infrastructure.queue import RedisJobQueue  # noqa: E402

DEFAULT_URL = "redis://:dev_only_not_a_secret@127.0.0.1:6382/0"


@pytest.fixture()
def client():
    url = os.environ.get("TEST_REDIS_URL", DEFAULT_URL)
    try:
        conn = redis.Redis.from_url(url, socket_connect_timeout=2)
        conn.ping()
    except redis.RedisError as exc:
        pytest.skip(f"Redis is not available for tests: {exc}")
    # A database of its own, flushed per test: these tests assert on list
    # lengths, and a leftover id from a previous test makes the failure look
    # like a queue bug.
    conn.flushdb()
    yield conn
    conn.flushdb()
    conn.close()


@pytest.fixture()
def queue(client) -> RedisJobQueue:
    return RedisJobQueue(client, namespace="test:opt", lease_seconds=2)


class TestSubmitAndClaim:
    def test_a_submitted_run_can_be_claimed(self, queue: RedisJobQueue) -> None:
        queue.submit("run-1")
        assert queue.pending_count() == 1

        lease = queue.claim("worker-a", timeout=1)
        assert lease is not None
        assert lease.run_id == "run-1"
        assert queue.pending_count() == 0
        assert queue.processing_count() == 1

    def test_claiming_an_empty_queue_returns_none(self, queue: RedisJobQueue) -> None:
        assert queue.claim("worker-a", timeout=1) is None

    def test_runs_are_claimed_in_submission_order(self, queue: RedisJobQueue) -> None:
        for i in range(3):
            queue.submit(f"run-{i}")
        claimed = [queue.claim("w", timeout=1).run_id for _ in range(3)]
        assert claimed == ["run-0", "run-1", "run-2"]

    def test_two_workers_never_claim_the_same_run(self, queue: RedisJobQueue) -> None:
        queue.submit("run-1")
        first = queue.claim("worker-a", timeout=1)
        second = queue.claim("worker-b", timeout=1)
        assert first is not None
        assert second is None

    def test_a_payload_is_a_hint_and_survives_the_claim(
        self, queue: RedisJobQueue
    ) -> None:
        queue.submit("run-1", {"seed": 7})
        queue.claim("worker-a", timeout=1)
        assert queue.payload("run-1") == {"seed": 7}

    def test_a_corrupt_payload_does_not_fail_the_run(
        self, queue: RedisJobQueue, client
    ) -> None:
        """The database row is authoritative; the payload is advisory."""
        queue.submit("run-1", {"seed": 7})
        client.set("test:opt:payload:run-1", b"{not json")
        assert queue.payload("run-1") is None


class TestAJobIsNeverLost:
    def test_a_claimed_run_stays_on_the_processing_list(
        self, queue: RedisJobQueue
    ) -> None:
        """`BRPOP` would delete the id the instant the worker received it, and
        a crash one microsecond later would lose the run silently."""
        queue.submit("run-1")
        queue.claim("worker-a", timeout=1)
        assert queue.processing_count() == 1
        assert "run-1" not in list(queue.drain_pending())

    def test_releasing_removes_it_from_processing(self, queue: RedisJobQueue) -> None:
        queue.submit("run-1")
        lease = queue.claim("worker-a", timeout=1)
        queue.release(lease)
        assert queue.processing_count() == 0
        assert queue.pending_count() == 0
        assert queue.payload("run-1") is None


class TestALeaseBoundsAJobThatOutlivesItsWorker:
    def test_a_live_lease_is_not_reclaimed(self, queue: RedisJobQueue) -> None:
        """Reclaiming a run whose worker is merely slow would run the same
        optimization twice and write two sets of results."""
        queue.submit("run-1")
        queue.claim("worker-a", timeout=1)
        assert queue.reclaim_expired() == []
        assert queue.processing_count() == 1

    def test_an_expired_lease_is_reclaimed(self, queue: RedisJobQueue) -> None:
        """The worker died. Redis expiring the key is the evidence that
        nobody has renewed it for a full TTL."""
        queue.submit("run-1")
        queue.claim("worker-a", timeout=1)
        time.sleep(2.5)  # lease_seconds=2
        assert queue.reclaim_expired() == ["run-1"]
        assert queue.pending_count() == 1
        assert queue.processing_count() == 0

    def test_a_reclaimed_run_can_be_claimed_again(self, queue: RedisJobQueue) -> None:
        queue.submit("run-1")
        queue.claim("worker-a", timeout=1)
        time.sleep(2.5)
        queue.reclaim_expired()
        again = queue.claim("worker-b", timeout=1)
        assert again is not None
        assert again.run_id == "run-1"

    def test_renewing_keeps_a_lease_alive(self, queue: RedisJobQueue) -> None:
        queue.submit("run-1")
        lease = queue.claim("worker-a", timeout=1)
        for _ in range(3):
            time.sleep(1)
            assert queue.renew(lease) is True
        assert queue.reclaim_expired() == []

    def test_renewing_a_lost_lease_reports_failure(
        self, queue: RedisJobQueue
    ) -> None:
        """The worker must treat this as "stop, somebody else owns this run",
        not as a transient error to retry through."""
        queue.submit("run-1")
        lease = queue.claim("worker-a", timeout=1)
        time.sleep(2.5)
        queue.reclaim_expired()
        queue.claim("worker-b", timeout=1)  # a new owner, a new token
        assert queue.renew(lease) is False

    def test_a_stalled_worker_cannot_release_the_new_owners_lease(
        self, queue: RedisJobQueue
    ) -> None:
        """The token exists for exactly this. Without it, a worker that woke
        up past its TTL would delete whichever worker legitimately owns the
        run now."""
        queue.submit("run-1")
        stale = queue.claim("worker-a", timeout=1)
        time.sleep(2.5)
        queue.reclaim_expired()
        fresh = queue.claim("worker-b", timeout=1)

        stale_worker_wakes_up = stale
        queue.release(stale_worker_wakes_up)

        assert queue.processing_count() == 1, "the new owner's claim was deleted"
        assert queue.renew(fresh) is True


class TestDiagnostics:
    def test_ping(self, queue: RedisJobQueue) -> None:
        assert queue.ping() is True

    def test_drain_pending_does_not_claim(self, queue: RedisJobQueue) -> None:
        queue.submit("run-1")
        assert list(queue.drain_pending()) == ["run-1"]
        assert queue.pending_count() == 1
