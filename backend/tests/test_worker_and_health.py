"""SESSION 20: the two modules that only ever run in production.

`worker.py` and `health.py` were at **0% coverage** while every other module
sat above 79%. That is the wrong way round. Nothing in the test suite runs
either of them, and nothing in development does either: the worker exists to
be the process in a container, and the healthcheck exists to be the thing
Docker calls every fifteen seconds. Both were verified by deploying and
watching.

The failures they are supposed to prevent are the expensive kind -- a worker
that starts and cannot solve, a healthcheck that passes while the database is
gone -- so they are exactly the modules that should not have been the
untested ones.
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Any

import pytest

from academic_ai import health, worker


# --------------------------------------------------------------------------
# health.py
# --------------------------------------------------------------------------


class TestHealthChecks:
    def test_a_missing_database_url_is_unhealthy(self, monkeypatch) -> None:
        monkeypatch.delenv("DATABASE_URL", raising=False)
        ok, detail = health._check_database()
        assert ok is False
        # Names the variable, so an operator reading `docker inspect` knows
        # what to set.
        assert "DATABASE_URL" in detail

    def test_a_reachable_database_is_healthy(self, monkeypatch, engine) -> None:
        monkeypatch.setenv("DATABASE_URL", str(engine.url.render_as_string(hide_password=False)))
        ok, detail = health._check_database()
        assert (ok, detail) == (True, "ok")

    def test_an_unreachable_database_reports_only_the_exception_type(
        self, monkeypatch
    ) -> None:
        # This string goes into container logs and `docker inspect` output,
        # which more people read than the .env file. The driver's own message
        # names the user it tried -- `password authentication failed for user
        # "academic_ai"` -- along with the host and port, so only the
        # exception *class name* is reported.
        monkeypatch.setenv(
            "DATABASE_URL",
            "postgresql+psycopg://someone:hunter2@127.0.0.1:1/nope",
        )
        ok, detail = health._check_database()
        assert ok is False
        # A bare identifier: no message, no host, no user, no DSN.
        assert detail.isidentifier(), detail

    def test_a_missing_redis_url_is_unhealthy(self, monkeypatch) -> None:
        monkeypatch.delenv("REDIS_URL", raising=False)
        ok, detail = health._check_redis()
        assert ok is False
        assert "REDIS_URL" in detail

    def test_an_unreachable_redis_reports_only_the_exception_type(
        self, monkeypatch
    ) -> None:
        # Same reason as the database check: redis-py's message carries the
        # host and port it tried.
        pytest.importorskip("redis")
        monkeypatch.setenv("REDIS_URL", "redis://:hunter2@127.0.0.1:1/0")
        ok, detail = health._check_redis()
        assert ok is False
        assert detail.isidentifier(), detail

    def test_the_optimizer_check_builds_a_real_model(self) -> None:
        # Importing `ortools` only proves the Python package is present. This
        # is the check that would have caught `ortools` going undeclared in
        # pyproject.toml from SESSION 3 to SESSION 8 -- an image that starts,
        # reports healthy, accepts jobs and fails every one of them.
        assert health._check_optimizer() == (True, "ok")


class TestHealthExitCode:
    """The property the module exists for: a healthcheck that always passes is
    worse than none, because compose then starts everything behind it."""

    def test_one_failing_check_makes_the_whole_process_unhealthy(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(health, "_check_settings", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_database", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_redis", lambda: (False, "boom"))

        assert health.main([]) == 1
        report = json.loads(capsys.readouterr().out)
        assert report["status"] == "unhealthy"
        # The reason is in the output, not only in the exit code.
        assert report["checks"]["redis"]["detail"] == "boom"

    def test_all_passing_checks_exit_zero(self, monkeypatch, capsys) -> None:
        monkeypatch.setattr(health, "_check_settings", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_database", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_redis", lambda: (True, "ok"))

        assert health.main([]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "ok"

    def test_the_worker_is_checked_for_something_the_api_is_not(
        self, monkeypatch, capsys
    ) -> None:
        # The API does not build CP-SAT models, so requiring OR-Tools of it
        # would make an API container unhealthy for a capability it never
        # uses.
        monkeypatch.setattr(health, "_check_settings", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_database", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_redis", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_optimizer", lambda: (True, "ok"))

        health.main(["--worker"])
        worker_report = json.loads(capsys.readouterr().out)
        health.main([])
        api_report = json.loads(capsys.readouterr().out)

        assert worker_report["role"] == "worker"
        assert "optimizer" in worker_report["checks"]
        assert api_report["role"] == "api"
        assert "optimizer" not in api_report["checks"]

    def test_a_broken_optimizer_makes_a_worker_unhealthy(
        self, monkeypatch, capsys
    ) -> None:
        monkeypatch.setattr(health, "_check_settings", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_database", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_redis", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_optimizer", lambda: (False, "ImportError"))

        assert health.main(["--worker"]) == 1
        capsys.readouterr()

    def test_the_report_is_one_json_object(self, monkeypatch, capsys) -> None:
        # Matching the structured logs, so a failing check in `docker inspect`
        # output is readable rather than a bare exit code.
        monkeypatch.setattr(health, "_check_settings", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_database", lambda: (True, "ok"))
        monkeypatch.setattr(health, "_check_redis", lambda: (True, "ok"))
        health.main([])
        out = capsys.readouterr().out.strip()
        assert len(out.splitlines()) == 1
        json.loads(out)


# --------------------------------------------------------------------------
# worker.py
# --------------------------------------------------------------------------


class FakeLease:
    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.token = "token"


class FakeQueue:
    """Enough queue to observe what the worker does with a lease."""

    def __init__(self, *, renew_returns: bool = True, release_raises: bool = False) -> None:
        self.released: list[str] = []
        self.renewals = 0
        self.reclaimed = 0
        self._renew_returns = renew_returns
        self._release_raises = release_raises

    def renew(self, lease: Any) -> bool:
        self.renewals += 1
        return self._renew_returns

    def release(self, lease: Any) -> None:
        if self._release_raises:
            import redis as _redis

            raise _redis.RedisError("gone")
        self.released.append(lease.run_id)

    def reclaim_expired(self) -> int:
        self.reclaimed += 1
        return 0


class FakeSession:
    def __init__(self) -> None:
        self.commits = 0

    def __enter__(self) -> "FakeSession":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def commit(self) -> None:
        self.commits += 1


class TestWorkerRefusesToStart:
    """Every one of these is a container that would otherwise start, report
    itself ready, and consume runs it cannot execute."""

    def test_invalid_configuration_refuses(self, monkeypatch) -> None:
        from academic_ai.security import settings as settings_module

        def boom() -> None:
            raise settings_module.ConfigurationError("SECRET_KEY is a placeholder")

        monkeypatch.setattr(worker, "configure_logging", lambda *a, **k: None)
        monkeypatch.setattr(settings_module, "load_security_settings", boom)
        assert worker.main() == 2

    def test_a_worker_that_cannot_build_a_model_refuses(self, monkeypatch) -> None:
        # Loudly, at deploy time, rather than at 02:00 when the first run is
        # submitted.
        from academic_ai.security import settings as settings_module

        monkeypatch.setattr(worker, "configure_logging", lambda *a, **k: None)
        monkeypatch.setattr(
            settings_module,
            "load_security_settings",
            lambda: _Settings(redis_url="redis://127.0.0.1:6379/0"),
        )
        monkeypatch.setattr(
            worker, "_verify_optimizer", _raise(RuntimeError("no native extension"))
        )
        assert worker.main() == 3

    def test_no_queue_refuses(self, monkeypatch) -> None:
        from academic_ai.security import settings as settings_module

        monkeypatch.setattr(worker, "configure_logging", lambda *a, **k: None)
        monkeypatch.setattr(
            settings_module, "load_security_settings", lambda: _Settings(redis_url="")
        )
        monkeypatch.setattr(worker, "_verify_optimizer", lambda: None)
        assert worker.main() == 4

    def test_the_optimizer_verification_builds_a_real_model(self) -> None:
        worker._verify_optimizer()  # raises if it cannot


class TestConcurrencyKnob:
    def test_a_number_is_honoured(self) -> None:
        assert worker.worker_concurrency("4") == 4

    def test_nonsense_means_one_rather_than_a_crash(self) -> None:
        # Refusing to boot over a malformed tuning knob is worse than
        # ignoring it.
        assert worker.worker_concurrency("eight") == 1
        assert worker.worker_concurrency("") == 1

    def test_zero_and_negatives_mean_one(self) -> None:
        # A worker configured to run zero runs is a worker that does nothing
        # while reporting healthy.
        assert worker.worker_concurrency("0") == 1
        assert worker.worker_concurrency("-3") == 1


class TestRunLease:
    def test_the_lease_is_released_after_a_successful_run(self) -> None:
        queue = FakeQueue()
        session = FakeSession()
        run_id = str(uuid.uuid4())

        worker.run_lease(
            FakeLease(run_id),
            queue=queue,
            make_session=lambda: session,
            worker_id="w1",
            execute=lambda *a, **k: _Outcome(),
        )

        assert queue.released == [run_id]
        assert session.commits == 1

    def test_the_lease_is_released_even_when_the_run_raises(self) -> None:
        # The guarantee this function was extracted to make assertable.
        # Leaving the id on the processing list makes the reaper requeue a run
        # that actually finished, and the second execution writes a second set
        # of results for the same request.
        queue = FakeQueue()
        run_id = str(uuid.uuid4())

        worker.run_lease(
            FakeLease(run_id),
            queue=queue,
            make_session=FakeSession,
            worker_id="w1",
            execute=_raise(RuntimeError("solver exploded")),
        )

        assert queue.released == [run_id]

    def test_a_run_that_raises_does_not_take_the_worker_down(self) -> None:
        # It returns rather than propagating: one poisoned run must not stop
        # the queue.
        worker.run_lease(
            FakeLease(str(uuid.uuid4())),
            queue=FakeQueue(),
            make_session=FakeSession,
            worker_id="w1",
            execute=_raise(ValueError("bad instance")),
        )

    def test_a_failure_to_release_is_survived(self) -> None:
        # Redis being gone at release time is bad, but crashing here loses the
        # log line that says which run it was.
        worker.run_lease(
            FakeLease(str(uuid.uuid4())),
            queue=FakeQueue(release_raises=True),
            make_session=FakeSession,
            worker_id="w1",
            execute=lambda *a, **k: _Outcome(),
        )


class TestHeartbeat:
    """The heartbeat handed to `execute_run`. Its answer decides whether
    results are written at all."""

    def _capture(self, queue: FakeQueue):
        captured: dict[str, Any] = {}

        def execute(session, run_id, *, worker_id, heartbeat):
            captured["heartbeat"] = heartbeat
            return _Outcome()

        worker.run_lease(
            FakeLease(str(uuid.uuid4())),
            queue=queue,
            make_session=FakeSession,
            worker_id="w1",
            execute=execute,
        )
        return captured["heartbeat"]

    def test_an_unforced_heartbeat_is_rate_limited(self) -> None:
        # Called from a solution callback, which can fire hundreds of times a
        # second. Renewing on every one is a Redis round trip per improvement.
        queue = FakeQueue()
        heartbeat = self._capture(queue)
        assert heartbeat() is True
        assert queue.renewals == 0

    def test_force_skips_the_rate_limit(self) -> None:
        # The runner forces one immediately before writing results: a fast
        # solve whose callback never fired would otherwise persist under a
        # lease it had already lost.
        queue = FakeQueue()
        heartbeat = self._capture(queue)
        assert heartbeat(force=True) is True
        assert queue.renewals == 1

    def test_a_lost_lease_is_reported_as_false(self) -> None:
        # False means another worker owns this run now, and the runner must
        # not write.
        queue = FakeQueue(renew_returns=False)
        heartbeat = self._capture(queue)
        assert heartbeat(force=True) is False


class TestReaper:
    def test_a_failing_reaper_pass_does_not_stop_the_worker(self) -> None:
        # Recovery failing must not stop the worker doing its actual job: a
        # Redis blip during a reaper pass would otherwise turn one abandoned
        # run into a stopped queue.
        worker.reap_once(FakeQueue(), _raise(RuntimeError("db gone")))

    def test_a_clean_pass_reclaims_expired_leases(self, engine, monkeypatch) -> None:
        """The happy path, against the real service and a real database.

        Worth running rather than asserting from a fake: `reap_once` swallows
        every exception, so a genuinely broken reaper -- a renamed method, a
        changed signature -- would be indistinguishable from a quiet one, and
        the symptom is runs from a dead worker that nobody ever requeues.
        """
        from academic_ai.infrastructure.db import session as session_module

        session_module.dispose_engine()
        try:
            monkeypatch.setenv(
                "DATABASE_URL", engine.url.render_as_string(hide_password=False)
            )
            queue = FakeQueue()
            worker.reap_once(queue, session_module.session_factory())
            assert queue.reclaimed == 1
        finally:
            session_module.dispose_engine()


# --------------------------------------------------------------------------


class _Outcome:
    class status:
        value = "SUCCEEDED"


class _Settings:
    def __init__(self, *, redis_url: str) -> None:
        self.redis_url = redis_url

    def redacted(self) -> dict[str, str]:
        return {"redis_url": "set" if self.redis_url else "unset"}


def _raise(exc: BaseException):
    def _fn(*args: object, **kwargs: object):
        raise exc

    return _fn


# --------------------------------------------------------------------------
# infrastructure/db/session.py — the third production-only module
# --------------------------------------------------------------------------


@pytest.fixture()
def clean_engine(engine):
    """Isolate the module-global engine.

    `get_engine` memoises one engine per process, which is right in a container
    and hazardous in a test suite: an engine left behind here would be handed
    to every later test. Disposed on both sides.
    """
    from academic_ai.infrastructure.db import session as session_module

    session_module.dispose_engine()
    yield session_module
    session_module.dispose_engine()


class TestSessionFactory:
    def test_no_database_url_refuses_rather_than_starting(
        self, clean_engine, monkeypatch
    ) -> None:
        # Both the API and the worker refuse to run without a database rather
        # than starting and failing on the first request -- which reads as an
        # application bug rather than a missing variable.
        monkeypatch.delenv("DATABASE_URL", raising=False)
        with pytest.raises(RuntimeError, match="DATABASE_URL"):
            clean_engine.get_engine()

    def test_one_engine_per_process(self, clean_engine, engine) -> None:
        # A second engine means a second pool, which on this shared host is
        # memory taken from other tenants to sit idle.
        url = engine.url.render_as_string(hide_password=False)
        first = clean_engine.get_engine(url)
        assert clean_engine.get_engine() is first

    def test_disposing_lets_a_new_engine_be_built(
        self, clean_engine, engine
    ) -> None:
        url = engine.url.render_as_string(hide_password=False)
        first = clean_engine.get_engine(url)
        clean_engine.dispose_engine()
        assert clean_engine.get_engine(url) is not first


class TestSessionScope:
    """The rollback is the point: a request that raises halfway through must
    not leave a partial write visible to the next one."""

    def _institutions(self, module, code: str) -> int:
        from sqlalchemy import func, select

        from academic_ai.infrastructure.db.models import Institution

        with module.session_factory()() as probe:
            return probe.scalar(
                select(func.count())
                .select_from(Institution)
                .where(Institution.code == code)
            )

    def test_a_successful_scope_commits(self, clean_engine, engine) -> None:
        from academic_ai.infrastructure.db.models import Institution

        clean_engine.get_engine(engine.url.render_as_string(hide_password=False))
        code = f"OK-{uuid.uuid4().hex[:8]}"

        scope = clean_engine.session_scope()
        db = next(scope)
        db.add(Institution(code=code, name="Comprometida"))
        next(scope, None)  # exhausting the generator runs the commit

        assert self._institutions(clean_engine, code) == 1

    def test_a_raising_scope_leaves_nothing_behind(
        self, clean_engine, engine
    ) -> None:
        # "The run row exists but was never queued" is exactly the
        # inconsistency this prevents -- a job nobody ever executes.
        from academic_ai.infrastructure.db.models import Institution

        clean_engine.get_engine(engine.url.render_as_string(hide_password=False))
        code = f"BAD-{uuid.uuid4().hex[:8]}"

        scope = clean_engine.session_scope()
        db = next(scope)
        db.add(Institution(code=code, name="Revertida"))
        db.flush()
        with pytest.raises(RuntimeError):
            scope.throw(RuntimeError("halfway through"))

        assert self._institutions(clean_engine, code) == 0


class TestSettingsCheck:
    def test_a_placeholder_secret_is_unhealthy(self, monkeypatch) -> None:
        monkeypatch.setenv("BACKEND_SECRET_KEY", "change-me")
        ok, detail = health._check_settings()
        assert ok is False
        # Names the variable, never the value: this string is printed to
        # stdout and read back by `docker inspect`.
        assert "change-me" not in detail

    def test_valid_settings_are_healthy(self, monkeypatch) -> None:
        import secrets

        monkeypatch.setenv("BACKEND_SECRET_KEY", secrets.token_hex(32))
        assert health._check_settings() == (True, "ok")

    def test_a_reachable_redis_is_healthy(self, monkeypatch) -> None:
        import os as _os

        pytest.importorskip("redis")
        url = _os.environ.get(
            "TEST_REDIS_URL", "redis://:dev_only_not_a_secret@127.0.0.1:6382/0"
        )
        monkeypatch.setenv("REDIS_URL", url)
        ok, detail = health._check_redis()
        if not ok:
            pytest.skip(f"Redis is not available for tests: {detail}")
        assert detail == "ok"


class TestTheProductionWiring:
    def test_run_lease_defaults_to_the_real_runner(self, engine, monkeypatch) -> None:
        """No `execute` argument: the path the container actually takes.

        Everything above injects a fake, so the import that wires the worker to
        `execute_run` was covered by nothing -- and a broken import there fails
        every run in production while every test passes. A run id that does not
        exist is enough: what is asserted is that the worker survives it and
        still releases the lease.
        """
        from academic_ai.infrastructure.db import session as session_module

        session_module.dispose_engine()
        try:
            monkeypatch.setenv(
                "DATABASE_URL", engine.url.render_as_string(hide_password=False)
            )
            queue = FakeQueue()
            run_id = str(uuid.uuid4())

            worker.run_lease(
                FakeLease(run_id),
                queue=queue,
                make_session=session_module.session_factory(),
                worker_id="w1",
            )

            assert queue.released == [run_id]
        finally:
            session_module.dispose_engine()
