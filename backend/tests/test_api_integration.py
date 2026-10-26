"""SESSION 9 integration: HTTP in, queue, worker, database, HTTP out.

These exercise the real path. The API is a real FastAPI app over the real
schema; the queue is a real Redis; the worker's executor is the real one and
runs a real CP-SAT solve. Nothing is mocked except the passage of time.

That matters because the interesting failures in an async job system are
failures *between* components -- a run queued before its row is committed, a
cancellation that arrives after the worker claimed the job, a lease that
expires mid-solve. None of those can be reproduced against a mock of either
side.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

redis_lib = pytest.importorskip("redis")

from academic_ai.api.app import create_app  # noqa: E402
from academic_ai.api.deps import (  # noqa: E402
    get_optimization_service,
    get_queue,
    get_session,
    get_settings,
)
from academic_ai.application.auth import AuthService  # noqa: E402
from academic_ai.application.optimization import (  # noqa: E402
    MAX_ATTEMPTS,
    OptimizationService,
)
from academic_ai.application.runner import execute_run  # noqa: E402
from academic_ai.domain.enums import (  # noqa: E402
    AcademicPeriodStatus,
    OptimizationRunStatus,
    UserRole,
)
from academic_ai.infrastructure.db.models import (  # noqa: E402
    AcademicPeriod,
    OptimizationRun,
    User,
    UserCredential,
)
from academic_ai.infrastructure.queue import RedisJobQueue  # noqa: E402
from academic_ai.security.passwords import hash_password  # noqa: E402
from academic_ai.security.settings import SecuritySettings  # noqa: E402

PASSWORD = "a perfectly reasonable passphrase"
REDIS_URL = os.environ.get(
    "TEST_REDIS_URL", "redis://:dev_only_not_a_secret@127.0.0.1:6382/1"
)


@pytest.fixture()
def settings() -> SecuritySettings:
    return SecuritySettings(
        secret_key="t" * 64,
        environment="test",
        allowed_hosts=(),
        cors_origins=(),
        access_token_minutes=30,
        refresh_token_days=7,
    )


@pytest.fixture()
def queue():
    try:
        client = redis_lib.Redis.from_url(REDIS_URL, socket_connect_timeout=2)
        client.ping()
    except redis_lib.RedisError as exc:
        pytest.skip(f"Redis is not available for tests: {exc}")
    client.flushdb()
    yield RedisJobQueue(client, namespace="itest:opt", lease_seconds=3)
    client.flushdb()
    client.close()


@pytest.fixture()
def coordinator(session: Session) -> User:
    user = User(
        email="coord@example.test",
        full_name="Coordinadora",
        primary_role=UserRole.COORDINATOR,
    )
    session.add(user)
    session.flush()
    session.add(
        UserCredential(app_user_id=user.id, password_hash=hash_password(PASSWORD))
    )
    session.flush()
    return user


@pytest.fixture()
def period(session: Session, world) -> AcademicPeriod:
    """A period with something in it to schedule.

    SESSION 18 replaced the fixture loader with a real repository read, and
    these tests went from exercising a hard-coded instance to exercising the
    database. An empty period is now correctly a 404 -- "no class sessions to
    schedule" -- so the fixture has to build one that can actually be solved.
    """
    from academic_ai.domain.enums import Modality, SessionType, TeacherRole
    from academic_ai.infrastructure.db.models import (
        ClassSession,
        CourseOffering,
        Section,
        SessionTeacher,
    )

    period = world.period

    offering = CourseOffering(
        course_id=world.course.id,
        academic_period_id=period.id,
        campus_id=world.campus.id,
        modality=Modality.ONSITE,
    )
    session.add(offering)
    session.flush()

    section = Section(
        course_offering_id=offering.id,
        academic_period_id=period.id,
        code="A",
        capacity=30,
    )
    session.add(section)
    session.flush()

    for index in (1, 2):
        class_session = ClassSession(
            section_id=section.id,
            academic_period_id=period.id,
            sequence_number=index,
            session_type=SessionType.LECTURE,
            delivery_mode=Modality.ONSITE,
            duration_slots=1,
        )
        session.add(class_session)
        session.flush()
        session.add(
            SessionTeacher(
                class_session_id=class_session.id,
                teacher_id=world.teacher.id,
                role=TeacherRole.PRIMARY,
            )
        )
    session.flush()
    return period


@pytest.fixture()
def workflows(session: Session, world):
    """The approval chains an institution configures.

    SESSION 12 built these against a seeded stub; SESSION 18 made the router
    read the database, so a period with nothing configured now correctly
    refuses with a 409. Configuring them here is what the institution would
    do, and the refusal path is still covered by its own test.
    """
    from academic_ai.domain.enums import ScheduleScope
    from academic_ai.infrastructure.db.models import ApprovalStep, ApprovalWorkflow

    definitions = (
        (
            "WITHIN_PROGRAM",
            ScheduleScope.PROGRAM,
            ((1, UserRole.COORDINATOR, False), (2, UserRole.DIRECTOR, False)),
        ),
        (
            "CROSS_PROGRAM",
            ScheduleScope.FACULTY,
            (
                (1, UserRole.COORDINATOR, True),
                (2, UserRole.DIRECTOR, False),
                (3, UserRole.DEAN, False),
            ),
        ),
    )
    for code, scope, steps in definitions:
        workflow = ApprovalWorkflow(
            institution_id=world.institution.id,
            code=code,
            name=code.replace("_", " ").title(),
            applies_to_scope=scope,
        )
        session.add(workflow)
        session.flush()
        for number, role, all_affected in steps:
            session.add(
                ApprovalStep(
                    approval_workflow_id=workflow.id,
                    step_number=number,
                    required_role=role,
                    requires_all_affected=all_affected,
                )
            )
    session.flush()
    # INSTITUTION is deliberately left unconfigured, so the refusal path a
    # cross-faculty change takes stays a real one.
    return definitions


@pytest.fixture()
def client(session: Session, queue: RedisJobQueue, settings: SecuritySettings):
    app = create_app(settings)
    # The test owns the transaction, so the app must use *this* session --
    # otherwise the rows the test creates are invisible to the request and the
    # request's writes are invisible to the test.
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_queue] = lambda: queue
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_optimization_service] = lambda: OptimizationService(
        session, queue
    )
    with TestClient(app) as c:
        yield c


def _login(client: TestClient) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": "coord@example.test", "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthEndToEnd:
    def test_login_returns_a_usable_access_token(
        self, client: TestClient, coordinator: User
    ) -> None:
        token = _login(client)
        me = client.get("/api/v1/auth/me", headers=_auth(token))
        assert me.status_code == 200
        assert me.json()["role"] == "COORDINATOR"
        assert "RUN_OPTIMIZATION" in me.json()["permissions"]

    def test_a_wrong_password_and_an_unknown_account_are_indistinguishable(
        self, client: TestClient, coordinator: User
    ) -> None:
        """Different answers here turn the login form into an account
        enumerator."""
        wrong = client.post(
            "/api/v1/auth/login",
            json={"email": "coord@example.test", "password": "not the password"},
        )
        unknown = client.post(
            "/api/v1/auth/login",
            json={"email": "nobody@example.test", "password": "not the password"},
        )
        assert wrong.status_code == unknown.status_code == 401
        assert wrong.json() == unknown.json()

    def test_no_token_is_401_not_403(self, client: TestClient) -> None:
        assert client.get("/api/v1/auth/me").status_code == 401

    def test_a_refresh_token_is_not_accepted_as_an_access_token(
        self, client: TestClient, coordinator: User
    ) -> None:
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "coord@example.test", "password": PASSWORD},
        ).json()
        response = client.get(
            "/api/v1/auth/me", headers=_auth(login["refresh_token"])
        )
        assert response.status_code == 401

    def test_refresh_rotates_and_the_old_token_stops_working(
        self, client: TestClient, coordinator: User
    ) -> None:
        """A refresh token that survives use can be replayed for its whole
        lifetime by whoever stole it."""
        first = client.post(
            "/api/v1/auth/login",
            json={"email": "coord@example.test", "password": PASSWORD},
        ).json()

        second = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert second.status_code == 200
        assert second.json()["refresh_token"] != first["refresh_token"]

        replay = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        )
        assert replay.status_code == 401

    def test_replaying_a_rotated_token_revokes_the_whole_session_family(
        self, client: TestClient, coordinator: User
    ) -> None:
        """A valid signature whose session row is revoked is evidence of
        theft, not a puzzle."""
        first = client.post(
            "/api/v1/auth/login",
            json={"email": "coord@example.test", "password": PASSWORD},
        ).json()
        second = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]}
        ).json()

        client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})

        # The token issued by the legitimate rotation is now dead too.
        after = client.post(
            "/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}
        )
        assert after.status_code == 401

    def test_logout_revokes_the_session(
        self, client: TestClient, coordinator: User
    ) -> None:
        login = client.post(
            "/api/v1/auth/login",
            json={"email": "coord@example.test", "password": PASSWORD},
        ).json()
        assert (
            client.post(
                "/api/v1/auth/logout", json={"refresh_token": login["refresh_token"]}
            ).status_code
            == 204
        )
        assert (
            client.post(
                "/api/v1/auth/refresh", json={"refresh_token": login["refresh_token"]}
            ).status_code
            == 401
        )

    def test_logout_with_a_junk_token_still_succeeds(
        self, client: TestClient
    ) -> None:
        """A client with a token it cannot use is exactly the client trying to
        discard it."""
        assert (
            client.post(
                "/api/v1/auth/logout", json={"refresh_token": "not-a-token"}
            ).status_code
            == 204
        )

    def test_repeated_failures_lock_the_account_with_a_retry_after(
        self, client: TestClient, coordinator: User
    ) -> None:
        for _ in range(6):
            response = client.post(
                "/api/v1/auth/login",
                json={"email": "coord@example.test", "password": "wrong"},
            )
        assert response.status_code == 429
        assert int(response.headers["Retry-After"]) > 0

        # The correct password does not get past a lock.
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"email": "coord@example.test", "password": PASSWORD},
            ).status_code
            == 429
        )

    def test_a_successful_login_clears_the_failure_count(
        self, client: TestClient, coordinator: User, session: Session
    ) -> None:
        for _ in range(3):
            client.post(
                "/api/v1/auth/login",
                json={"email": "coord@example.test", "password": "wrong"},
            )
        _login(client)
        credential = session.query(UserCredential).filter_by(
            app_user_id=coordinator.id
        ).one()
        assert credential.failed_attempts == 0
        assert credential.locked_until is None


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


class TestAuthorisationIsEnforcedAtTheEdge:
    def test_a_student_may_not_submit_a_run(
        self, client: TestClient, session: Session, period: AcademicPeriod
    ) -> None:
        student = User(
            email="student@example.test", full_name="E", primary_role=UserRole.STUDENT
        )
        session.add(student)
        session.flush()
        session.add(
            UserCredential(
                app_user_id=student.id, password_hash=hash_password(PASSWORD)
            )
        )
        session.flush()

        token = client.post(
            "/api/v1/auth/login",
            json={"email": "student@example.test", "password": PASSWORD},
        ).json()["access_token"]

        response = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id)},
            headers=_auth(token),
        )
        assert response.status_code == 403, "a student ran an optimization"

    def test_a_demotion_takes_effect_on_the_next_request(
        self,
        client: TestClient,
        coordinator: User,
        session: Session,
        period: AcademicPeriod,
    ) -> None:
        """The role is read from the database, not from the token. Otherwise a
        token minted before a demotion keeps its privileges until it expires."""
        token = _login(client)
        assert (
            client.post(
                "/api/v1/optimization/runs",
                json={"academic_period_id": str(period.id)},
                headers=_auth(token),
            ).status_code
            == 202
        )

        coordinator.primary_role = UserRole.STUDENT
        session.flush()

        assert (
            client.post(
                "/api/v1/optimization/runs",
                json={"academic_period_id": str(period.id)},
                headers=_auth(token),
            ).status_code
            == 403
        )

    def test_a_deactivated_user_is_rejected_immediately(
        self, client: TestClient, coordinator: User, session: Session
    ) -> None:
        token = _login(client)
        coordinator.is_active = False
        session.flush()
        assert client.get("/api/v1/auth/me", headers=_auth(token)).status_code == 401


# ---------------------------------------------------------------------------
# Optimization must not block HTTP
# ---------------------------------------------------------------------------


class TestSubmissionIsAsynchronous:
    def test_submitting_returns_202_without_running_anything(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        queue: RedisJobQueue,
        session: Session,
    ) -> None:
        token = _login(client)
        response = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id), "time_limit_seconds": 5},
            headers=_auth(token),
        )
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "QUEUED"

        run = session.get(OptimizationRun, uuid.UUID(body["run_id"]))
        assert run.status is OptimizationRunStatus.QUEUED
        assert run.started_at is None, "the request performed the optimization"
        assert queue.pending_count() == 1

    def test_the_row_exists_before_the_id_is_queued(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        queue: RedisJobQueue,
        session: Session,
    ) -> None:
        """A worker fast enough to claim the id must not find a missing row."""
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id)},
            headers=_auth(token),
        ).json()["run_id"]
        assert list(queue.drain_pending()) == [run_id]
        assert session.get(OptimizationRun, uuid.UUID(run_id)) is not None

    def test_an_absurd_time_limit_is_refused(
        self, client: TestClient, coordinator: User, period: AcademicPeriod
    ) -> None:
        """Unbounded is a way to hold 8 CPU on a shared host indefinitely."""
        token = _login(client)
        response = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id), "time_limit_seconds": 999999},
            headers=_auth(token),
        )
        assert response.status_code == 422


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


class TestWorkerExecution:
    def test_a_queued_run_is_executed_to_completion(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        queue: RedisJobQueue,
        session: Session,
    ) -> None:
        """The whole path: submit over HTTP, claim from Redis, solve with
        CP-SAT, persist, read back over HTTP."""
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id), "time_limit_seconds": 10},
            headers=_auth(token),
        ).json()["run_id"]

        lease = queue.claim("test-worker", timeout=2)
        assert lease is not None and lease.run_id == run_id

        outcome = execute_run(session, uuid.UUID(run_id), worker_id="test-worker")
        assert outcome.status is OptimizationRunStatus.COMPLETED, outcome.detail

        response = client.get(
            f"/api/v1/optimization/runs/{run_id}", headers=_auth(token)
        )
        body = response.json()
        assert body["status"] == "COMPLETED"
        assert body["progress_percent"] == 100
        assert body["is_terminal"] is True
        assert body["solver_status"] in {"OPTIMAL", "FEASIBLE"}
        assert body["started_at"] is not None
        assert body["finished_at"] is not None

    def test_execution_records_a_heartbeat_and_the_worker_id(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        session: Session,
    ) -> None:
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id), "time_limit_seconds": 5},
            headers=_auth(token),
        ).json()["run_id"]

        execute_run(session, uuid.UUID(run_id), worker_id="worker-42")
        run = session.get(OptimizationRun, uuid.UUID(run_id))
        assert run.worker_id == "worker-42"
        assert run.heartbeat_at is not None
        assert run.attempts == 1

    def test_metrics_are_persisted_per_tier(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        session: Session,
    ) -> None:
        """Structured rows, so comparing runs is a query rather than parsing."""
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id), "time_limit_seconds": 10},
            headers=_auth(token),
        ).json()["run_id"]

        execute_run(session, uuid.UUID(run_id), worker_id="w")
        run = session.get(OptimizationRun, uuid.UUID(run_id))
        assert run.metrics, "no per-tier metrics were recorded"
        assert all(m.name.startswith("tier.") for m in run.metrics)

    def test_a_lost_lease_stops_the_run_without_writing_results(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        session: Session,
    ) -> None:
        """Two workers writing results for one run is what the lease exists to
        prevent."""
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id), "time_limit_seconds": 10},
            headers=_auth(token),
        ).json()["run_id"]

        outcome = execute_run(
            session,
            uuid.UUID(run_id),
            worker_id="w",
            heartbeat=lambda force=False: False,
        )
        assert outcome.detail == "lease lost"
        run = session.get(OptimizationRun, uuid.UUID(run_id))
        assert run.status is not OptimizationRunStatus.COMPLETED
        assert run.finished_at is None, "results were written under a lost lease"


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancellation:
    def test_cancelling_a_queued_run_terminates_it_immediately(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        session: Session,
    ) -> None:
        """Nobody has claimed it, so there is no cooperative party to wait
        for."""
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id)},
            headers=_auth(token),
        ).json()["run_id"]

        response = client.post(
            f"/api/v1/optimization/runs/{run_id}/cancel", headers=_auth(token)
        )
        assert response.status_code == 200
        assert response.json()["status"] == "CANCELLED"

        run = session.get(OptimizationRun, uuid.UUID(run_id))
        assert run.cancel_requested_by_user_id == coordinator.id
        assert run.finished_at is not None

    def test_a_cancelled_run_is_not_executed_if_a_worker_claims_it(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        session: Session,
    ) -> None:
        """The race that matters: cancel lands, then a worker picks the id off
        the queue. Executing it would restart a run somebody stopped."""
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id)},
            headers=_auth(token),
        ).json()["run_id"]
        client.post(f"/api/v1/optimization/runs/{run_id}/cancel", headers=_auth(token))

        outcome = execute_run(session, uuid.UUID(run_id), worker_id="w")
        assert outcome.status is OptimizationRunStatus.CANCELLED

        run = session.get(OptimizationRun, uuid.UUID(run_id))
        assert run.solver_status is None, "a cancelled run was solved anyway"

    def test_cancelling_a_finished_run_is_a_success_not_a_conflict(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        session: Session,
    ) -> None:
        """Two coordinators pressing cancel should both be told it is done."""
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id), "time_limit_seconds": 5},
            headers=_auth(token),
        ).json()["run_id"]
        execute_run(session, uuid.UUID(run_id), worker_id="w")

        response = client.post(
            f"/api/v1/optimization/runs/{run_id}/cancel", headers=_auth(token)
        )
        assert response.status_code == 200
        assert response.json()["status"] == "COMPLETED"
        assert "already finished" in response.json()["detail"]

    def test_cancelling_an_unknown_run_is_404(
        self, client: TestClient, coordinator: User
    ) -> None:
        token = _login(client)
        response = client.post(
            f"/api/v1/optimization/runs/{uuid.uuid4()}/cancel", headers=_auth(token)
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Recovery
# ---------------------------------------------------------------------------


class TestAbandonedRuns:
    def test_a_run_whose_worker_died_is_requeued(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        session: Session,
        queue: RedisJobQueue,
    ) -> None:
        """Otherwise it sits in OPTIMIZING forever, indistinguishable from one
        that is merely slow."""
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id)},
            headers=_auth(token),
        ).json()["run_id"]

        run = session.get(OptimizationRun, uuid.UUID(run_id))
        run.status = OptimizationRunStatus.OPTIMIZING
        run.worker_id = "a worker that is no longer with us"
        run.heartbeat_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        run.attempts = 1
        session.flush()

        reaped = OptimizationService(session, queue).reap_abandoned()
        assert uuid.UUID(run_id) in reaped

        run = session.get(OptimizationRun, uuid.UUID(run_id))
        assert run.status is OptimizationRunStatus.QUEUED
        assert run.worker_id is None

    def test_a_run_that_has_killed_enough_workers_is_failed_not_retried(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        session: Session,
        queue: RedisJobQueue,
    ) -> None:
        """A run that killed three workers is a bug report, not a retry."""
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id)},
            headers=_auth(token),
        ).json()["run_id"]

        run = session.get(OptimizationRun, uuid.UUID(run_id))
        run.status = OptimizationRunStatus.OPTIMIZING
        run.heartbeat_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
        run.attempts = MAX_ATTEMPTS
        session.flush()

        OptimizationService(session, queue).reap_abandoned()
        run = session.get(OptimizationRun, uuid.UUID(run_id))
        assert run.status is OptimizationRunStatus.FAILED
        assert "not retried again" in run.failure_reason

    def test_a_healthy_run_is_not_reaped(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        session: Session,
        queue: RedisJobQueue,
    ) -> None:
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id)},
            headers=_auth(token),
        ).json()["run_id"]
        run = session.get(OptimizationRun, uuid.UUID(run_id))
        run.status = OptimizationRunStatus.OPTIMIZING
        run.heartbeat_at = dt.datetime.now(dt.timezone.utc)
        session.flush()

        assert OptimizationService(session, queue).reap_abandoned() == []


# ---------------------------------------------------------------------------
# Progress stream (ADR-0011)
# ---------------------------------------------------------------------------


class TestProgressStream:
    def test_a_finished_run_streams_its_state_and_closes(
        self,
        client: TestClient,
        coordinator: User,
        period: AcademicPeriod,
        session: Session,
    ) -> None:
        """The stream must terminate on a terminal status. One that idles
        forever is a slow memory leak with a sympathetic explanation."""
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id), "time_limit_seconds": 5},
            headers=_auth(token),
        ).json()["run_id"]
        execute_run(session, uuid.UUID(run_id), worker_id="w")

        with client.stream(
            "GET",
            f"/api/v1/optimization/runs/{run_id}/events",
            headers=_auth(token),
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-accel-buffering"] == "no"
            body = "".join(response.iter_text())

        assert "event: progress" in body
        assert "event: done" in body
        assert '"status":"COMPLETED"' in body.replace(" ", "")

    def test_the_stream_requires_authentication(
        self, client: TestClient, coordinator: User, period: AcademicPeriod
    ) -> None:
        token = _login(client)
        run_id = client.post(
            "/api/v1/optimization/runs",
            json={"academic_period_id": str(period.id)},
            headers=_auth(token),
        ).json()["run_id"]
        response = client.get(f"/api/v1/optimization/runs/{run_id}/events")
        assert response.status_code == 401

    def test_streaming_an_unknown_run_is_404(
        self, client: TestClient, coordinator: User
    ) -> None:
        token = _login(client)
        response = client.get(
            f"/api/v1/optimization/runs/{uuid.uuid4()}/events", headers=_auth(token)
        )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# SESSION 10: the drag preview
# ---------------------------------------------------------------------------


class TestSchedulePreview:
    """ADR-0012: a drag is a proposal, and the preview endpoint writes
    nothing. These check that over HTTP, where the client actually meets it."""

    def test_the_schedule_view_returns_domains_not_just_placements(
        self, client: TestClient, coordinator: User, period: AcademicPeriod
    ) -> None:
        """The client renders legal drop targets from the server's domains.
        Without them it would have to derive feasibility itself, and that
        second implementation would drift from CP-SAT."""
        token = _login(client)
        response = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}", headers=_auth(token)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["domains"], "no domains returned; the client cannot know where to drop"
        first = body["domains"][0]
        assert first["start_slot_ids"]
        assert "is_locked" in first

    def test_impact_returns_the_numbers_the_preview_needs(
        self, client: TestClient, coordinator: User, period: AcademicPeriod
    ) -> None:
        token = _login(client)
        view = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}", headers=_auth(token)
        ).json()
        domain = view["domains"][0]
        session_id = domain["session_id"]

        placement = [
            {
                "session_id": session_id,
                "start_slot_id": domain["start_slot_ids"][0],
                "room_id": domain["room_ids"][0] if domain["room_ids"] else None,
            }
        ]
        target = domain["start_slot_ids"][-1]

        response = client.post(
            "/api/v1/schedule/impact",
            json={
                "academic_period_id": str(period.id),
                "placement": placement,
                "moves": [{"session_id": session_id, "to_start_slot_id": target}],
            },
            headers=_auth(token),
        )
        assert response.status_code == 200, response.text
        body = response.json()

        # Every field the playbook names.
        for field in (
            "affected", "new_conflicts", "resolved_conflicts", "score_delta",
            "tier_codes", "stability_delta", "summary",
        ):
            assert field in body, f"the preview cannot show {field}"
        for who in ("students", "teachers", "programs", "faculties", "rooms"):
            assert who in body["affected"]

        # Per tier, never a scalar.
        assert isinstance(body["score_delta"], list)
        assert len(body["score_delta"]) == len(body["tier_codes"])

    def test_impact_refuses_a_move_for_a_session_it_was_given_no_placement_for(
        self, client: TestClient, coordinator: User, period: AcademicPeriod
    ) -> None:
        """Silently ignoring it would return an impact of zero for a move the
        user believes was analysed."""
        token = _login(client)
        response = client.post(
            "/api/v1/schedule/impact",
            json={
                "academic_period_id": str(period.id),
                "placement": [],
                "moves": [{"session_id": "GHOST", "to_start_slot_id": "mon0"}],
            },
            headers=_auth(token),
        )
        assert response.status_code == 422
        assert "GHOST" in response.json()["detail"]

    def test_an_unbounded_batch_of_moves_is_refused(
        self, client: TestClient, coordinator: User, period: AcademicPeriod
    ) -> None:
        """Impact analysis is O(students x pairs); an unbounded batch is a way
        to hold a worker on a shared host."""
        token = _login(client)
        response = client.post(
            "/api/v1/schedule/impact",
            json={
                "academic_period_id": str(period.id),
                "placement": [],
                "moves": [
                    {"session_id": f"S{i}", "to_start_slot_id": "mon0"}
                    for i in range(200)
                ],
            },
            headers=_auth(token),
        )
        assert response.status_code == 422

    def test_a_student_may_not_analyse_impact(
        self, client: TestClient, session: Session, period: AcademicPeriod
    ) -> None:
        student = User(
            email="s2@example.test", full_name="E", primary_role=UserRole.STUDENT
        )
        session.add(student)
        session.flush()
        session.add(
            UserCredential(app_user_id=student.id, password_hash=hash_password(PASSWORD))
        )
        session.flush()
        token = client.post(
            "/api/v1/auth/login",
            json={"email": "s2@example.test", "password": PASSWORD},
        ).json()["access_token"]

        response = client.post(
            "/api/v1/schedule/impact",
            json={"academic_period_id": str(period.id), "placement": [], "moves": []},
            headers=_auth(token),
        )
        assert response.status_code == 403

    def test_there_is_no_endpoint_that_applies_a_move(
        self, client: TestClient, coordinator: User
    ) -> None:
        """The rule ADR-0012 marks CRITICAL, checked at the routing table: no
        route under /schedule writes a placement. Applying a change is a
        ChangeRequest and a separate approval."""
        token = _login(client)
        spec = client.get("/openapi.json").json()
        schedule_paths = {
            path: sorted(methods)
            for path, methods in spec["paths"].items()
            if path.startswith("/api/v1/schedule")
        }
        assert schedule_paths == {
            "/api/v1/schedule": ["get"],
            "/api/v1/schedule/impact": ["post"],
            "/api/v1/schedule/alternatives": ["post"],
        }, f"a schedule-mutating route appeared: {schedule_paths}"


    def test_alternatives_are_ranked_and_apply_nothing(
        self, client: TestClient, coordinator: User, period: AcademicPeriod
    ) -> None:
        """SIMULATE ALTERNATIVES from ADR-0012. Every option comes from the
        session's domain, so each is one the solver would accept, and choosing
        one still only swaps the proposal."""
        token = _login(client)
        view = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}", headers=_auth(token)
        ).json()
        domain = view["domains"][0]
        session_id = domain["session_id"]
        placement = [
            {
                "session_id": session_id,
                "start_slot_id": domain["start_slot_ids"][0],
                "room_id": domain["room_ids"][0] if domain["room_ids"] else None,
            }
        ]

        response = client.post(
            "/api/v1/schedule/alternatives",
            json={
                "academic_period_id": str(period.id),
                "placement": placement,
                "moves": [
                    {
                        "session_id": session_id,
                        "to_start_slot_id": domain["start_slot_ids"][-1],
                    }
                ],
            },
            headers=_auth(token),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        for alternative in body["alternatives"]:
            # Only slots the server said were legal.
            assert (
                alternative["moves"][0]["to_start_slot_id"]
                in domain["start_slot_ids"]
            )
            assert "score_delta" in alternative["impact"]

    def test_alternatives_require_a_proposal_to_be_alternative_to(
        self, client: TestClient, coordinator: User, period: AcademicPeriod
    ) -> None:
        token = _login(client)
        response = client.post(
            "/api/v1/schedule/alternatives",
            json={
                "academic_period_id": str(period.id),
                "placement": [],
                "moves": [],
            },
            headers=_auth(token),
        )
        assert response.status_code == 422

    def test_alternatives_are_stable_across_identical_requests(
        self, client: TestClient, coordinator: User, period: AcademicPeriod
    ) -> None:
        """A ranking that reshuffles between two identical calls reads as a
        bug, and makes a coordinator distrust the whole panel."""
        token = _login(client)
        view = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}", headers=_auth(token)
        ).json()
        domain = view["domains"][0]
        body = {
            "academic_period_id": str(period.id),
            "placement": [
                {
                    "session_id": domain["session_id"],
                    "start_slot_id": domain["start_slot_ids"][0],
                    "room_id": domain["room_ids"][0] if domain["room_ids"] else None,
                }
            ],
            "moves": [
                {
                    "session_id": domain["session_id"],
                    "to_start_slot_id": domain["start_slot_ids"][-1],
                }
            ],
        }
        first = client.post("/api/v1/schedule/alternatives", json=body, headers=_auth(token)).json()
        second = client.post("/api/v1/schedule/alternatives", json=body, headers=_auth(token)).json()
        assert first == second


# ---------------------------------------------------------------------------
# SESSION 12: governance routing
# ---------------------------------------------------------------------------


class TestGovernanceRouting:
    """The chain is shown *before* submission, and refused when the
    institution has not configured one."""

    @staticmethod
    def _impact(**affected) -> dict:
        counts = {
            "students": 0, "teachers": 0, "programs": 1,
            "faculties": 1, "rooms": 0, "sessions": 1,
        }
        counts.update(affected)
        return {
            "moves": [],
            "affected": counts,
            "new_conflicts": [],
            "resolved_conflicts": [],
            "score_delta": [0, 0],
            "tier_codes": ["A", "B"],
            "stability_delta": 0,
            "occupancy_clashes": [],
            "summary": "",
            "is_improvement": False,
            "is_neutral": True,
        }

    def test_a_within_programme_change_gets_the_programme_chain(
        self, client: TestClient, coordinator: User, workflows
    ) -> None:
        token = _login(client)
        response = client.post(
            "/api/v1/governance/routing", json=self._impact(), headers=_auth(token)
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["assessment"]["scope"] == "PROGRAM"
        assert [s["required_role"] for s in body["steps"]] == [
            "COORDINATOR",
            "DIRECTOR",
        ]

    def test_a_cross_programme_change_needs_every_affected_coordinator(
        self, client: TestClient, coordinator: User, workflows
    ) -> None:
        """The playbook's example: "affected Coordinators", plural."""
        token = _login(client)
        body = client.post(
            "/api/v1/governance/routing",
            json=self._impact(programs=3),
            headers=_auth(token),
        ).json()
        assert body["assessment"]["scope"] == "FACULTY"
        first = body["steps"][0]
        assert first["required_role"] == "COORDINATOR"
        assert first["requires_all_affected"] is True

    def test_a_cross_faculty_change_is_refused_until_somebody_configures_it(
        self, client: TestClient, coordinator: User
    ) -> None:
        """No default chain. A fallback would route every cross-faculty change
        in the institution through whichever chain happened to be nearest, and
        it would look exactly like routing them correctly."""
        token = _login(client)
        response = client.post(
            "/api/v1/governance/routing",
            json=self._impact(programs=4, faculties=2),
            headers=_auth(token),
        )
        assert response.status_code == 409
        assert "no approval workflow is configured" in response.json()["detail"]

    def test_the_routing_explains_what_widened_it(
        self, client: TestClient, coordinator: User, workflows
    ) -> None:
        token = _login(client)
        body = client.post(
            "/api/v1/governance/routing",
            json=self._impact(programs=3),
            headers=_auth(token),
        ).json()
        assert "programmes" in body["assessment"]["rationale"]

    def test_a_student_may_not_ask_who_approves_things(
        self, client: TestClient, session: Session
    ) -> None:
        student = User(
            email="s3@example.test", full_name="E", primary_role=UserRole.STUDENT
        )
        session.add(student)
        session.flush()
        session.add(
            UserCredential(app_user_id=student.id, password_hash=hash_password(PASSWORD))
        )
        session.flush()
        token = client.post(
            "/api/v1/auth/login",
            json={"email": "s3@example.test", "password": PASSWORD},
        ).json()["access_token"]

        response = client.post(
            "/api/v1/governance/routing", json=self._impact(), headers=_auth(token)
        )
        assert response.status_code == 403


class TestScheduleLabels:
    """SESSION 22: the grid stops naming classes by UUID.

    `GET /schedule` carried placement and domains and nothing a person could
    read. The frontend fabricated a `SessionMeta` from the session id, so
    every class on the timetable was labelled with a UUID from SESSION 10 to
    SESSION 22.
    """

    def test_the_response_names_each_session(
        self, client: TestClient, coordinator: User, period, world
    ) -> None:
        token = _login(client)
        body = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}",
            headers=_auth(token),
        ).json()

        assert body["sessions"], "no session metadata returned"
        meta = body["sessions"][0]
        assert meta["course_code"] == world.course.code
        assert meta["section_code"] == "A"
        assert meta["teacher_names"] == [world.teacher.full_name]
        # The label is not the id, which is the entire point.
        assert meta["course_code"] != meta["session_id"]

    def test_every_session_in_the_domains_has_a_label(
        self, client: TestClient, coordinator: User, period
    ) -> None:
        # A partial answer is worse than none here: half the grid would read
        # as course codes and half as UUIDs, and nobody could tell which half
        # was the bug.
        token = _login(client)
        body = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}",
            headers=_auth(token),
        ).json()

        labelled = {s["session_id"] for s in body["sessions"]}
        placed = {d["session_id"] for d in body["domains"]}
        assert placed <= labelled

    def test_labels_do_not_change_the_domains(
        self, client: TestClient, coordinator: User, period
    ) -> None:
        # The label read is a second, separate query. If it could alter what
        # the solver is told, it would have made a display concern into a
        # feasibility one.
        token = _login(client)
        body = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}",
            headers=_auth(token),
        ).json()
        for domain in body["domains"]:
            assert domain["start_slot_ids"], "a session lost its domain"


class TestScheduleGrid:
    """SESSION 29: the week the grid draws.

    The client derived the day and the row from the *text* of the slot id with
    `/^([a-z]+)(\d+)$/`, which matches the fixtures' `mon0` and nothing a
    database produces. Against real UUIDs every slot parsed as day "DAY", row
    0 — so the whole week collapsed into one cell and all sixteen classes
    landed in it. The timetable rendered, and was useless.
    """

    def test_the_response_carries_the_grid(
        self, client: TestClient, coordinator: User, period
    ) -> None:
        token = _login(client)
        body = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}",
            headers=_auth(token),
        ).json()

        assert body["slots"], "no grid returned"
        slot = body["slots"][0]
        assert slot["day"] in {
            "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY",
            "SATURDAY", "SUNDAY",
        }
        assert isinstance(slot["in_day_index"], int)
        # A time somebody can read in the left-hand column.
        assert ":" in slot["label"]

    def test_the_grid_matches_the_period_row_for_row(
        self, client: TestClient, coordinator: User, period, session: Session
    ) -> None:
        """The property the old bug broke: the grid *is* the period's grid.

        Asserting "more than one day" would describe the fixture rather than
        the code. What matters is that day and index come from the timeslot
        rows — parsing them out of a UUID gave ("DAY", 0) for every slot, and
        the shape of that failure is exactly a grid that no longer matches
        what the period holds.
        """
        from sqlalchemy import select

        from academic_ai.infrastructure.db.models import Timeslot

        token = _login(client)
        body = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}",
            headers=_auth(token),
        ).json()

        expected = {
            str(t.id): (t.day_of_week.value, t.day_slot_index)
            for t in session.scalars(
                select(Timeslot).where(Timeslot.academic_period_id == period.id)
            )
        }
        actual = {s["slot_id"]: (s["day"], s["in_day_index"]) for s in body["slots"]}
        assert actual == expected

    def test_distinct_slots_stay_distinct_in_the_grid(
        self, client: TestClient, coordinator: User, period
    ) -> None:
        # The collapse, stated directly: N slots must produce N cells. Parsing
        # UUIDs mapped every one of them onto the same (day, row), so a week
        # of thirty slots drew a single cell holding every class.
        token = _login(client)
        body = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}",
            headers=_auth(token),
        ).json()

        cells = {(s["day"], s["in_day_index"]) for s in body["slots"]}
        assert len(cells) == len(body["slots"])

    def test_every_slot_a_domain_mentions_is_in_the_grid(
        self, client: TestClient, coordinator: User, period
    ) -> None:
        # A session may be placed only where the grid can draw it. A domain
        # naming a slot the grid does not carry is a class with nowhere to
        # appear.
        token = _login(client)
        body = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}",
            headers=_auth(token),
        ).json()

        drawn = {s["slot_id"] for s in body["slots"]}
        for domain in body["domains"]:
            assert set(domain["start_slot_ids"]) <= drawn

    def test_labels_are_ordered_by_the_index_they_carry(
        self, client: TestClient, coordinator: User, period
    ) -> None:
        # The row order comes from `in_day_index`, not from sorting the label
        # as text — which would put 10:00 before 09:00.
        token = _login(client)
        body = client.get(
            f"/api/v1/schedule?academic_period_id={period.id}",
            headers=_auth(token),
        ).json()

        for day in {s["day"] for s in body["slots"]}:
            of_day = sorted(
                (s for s in body["slots"] if s["day"] == day),
                key=lambda s: s["in_day_index"],
            )
            minutes = [s["start_minute"] for s in of_day]
            assert minutes == sorted(minutes), f"{day} is out of order"


class TestAForcedPasswordChange:
    """SESSION 33: `must_change` finally means something.

    It existed on the credential from SESSION 8, was reported at login, and
    was enforced nowhere — so a temporary password an administrator issued
    worked indefinitely, which is not temporary. There was also no endpoint
    that could clear it.
    """

    @pytest.fixture()
    def admin_token(self, client: TestClient, session: Session) -> str:
        """An administrator, because `_login` signs in as a coordinator and
        `MANAGE_USERS` belongs to institution-wide roles only."""
        user = User(
            email="useradmin@example.test",
            full_name="Administradora",
            primary_role=UserRole.ADMIN,
        )
        session.add(user)
        session.flush()
        session.add(
            UserCredential(
                app_user_id=user.id, password_hash=hash_password(PASSWORD)
            )
        )
        session.flush()
        response = client.post(
            "/api/v1/auth/login",
            json={"email": "useradmin@example.test", "password": PASSWORD},
        )
        assert response.status_code == 200, response.text
        return response.json()["access_token"]

    def _new_account(self, client: TestClient, token: str) -> dict:
        response = client.post(
            "/api/v1/users",
            headers=_auth(token),
            json={
                "email": "recien@example.test",
                "full_name": "Recién Llegada",
                "role": "COORDINATOR",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    def test_a_new_account_can_sign_in(
        self, client: TestClient, admin_token: str
    ) -> None:
        account = self._new_account(client, admin_token)

        response = client.post(
            "/api/v1/auth/login",
            json={
                "email": "recien@example.test",
                "password": account["temporary_password"],
            },
        )
        assert response.status_code == 200
        assert response.json()["must_change_password"] is True

    def test_but_can_do_nothing_else_until_it_changes(
        self, client: TestClient, admin_token: str
    ) -> None:
        account = self._new_account(client, admin_token)
        token = client.post(
            "/api/v1/auth/login",
            json={
                "email": "recien@example.test",
                "password": account["temporary_password"],
            },
        ).json()["access_token"]

        blocked = client.get("/api/v1/optimization/runs", headers=_auth(token))
        assert blocked.status_code == 403
        assert "change-password" in blocked.json()["detail"]

    def test_the_endpoint_that_clears_it_stays_reachable(
        self, client: TestClient, admin_token: str
    ) -> None:
        # Refusing this one too would lock the account out permanently, which
        # is why it depends on `get_principal` rather than on `require()`.
        account = self._new_account(client, admin_token)
        token = client.post(
            "/api/v1/auth/login",
            json={
                "email": "recien@example.test",
                "password": account["temporary_password"],
            },
        ).json()["access_token"]

        assert client.get("/api/v1/auth/me", headers=_auth(token)).status_code == 200
        changed = client.post(
            "/api/v1/auth/change-password",
            headers=_auth(token),
            json={
                "current_password": account["temporary_password"],
                "new_password": "una-contrasena-nueva-y-larga",
            },
        )
        assert changed.status_code == 204, changed.text

    def test_and_afterwards_the_platform_opens(
        self, client: TestClient, admin_token: str
    ) -> None:
        account = self._new_account(client, admin_token)
        first = client.post(
            "/api/v1/auth/login",
            json={
                "email": "recien@example.test",
                "password": account["temporary_password"],
            },
        ).json()["access_token"]
        client.post(
            "/api/v1/auth/change-password",
            headers=_auth(first),
            json={
                "current_password": account["temporary_password"],
                "new_password": "una-contrasena-nueva-y-larga",
            },
        )

        again = client.post(
            "/api/v1/auth/login",
            json={
                "email": "recien@example.test",
                "password": "una-contrasena-nueva-y-larga",
            },
        )
        assert again.json()["must_change_password"] is False
        token = again.json()["access_token"]
        assert (
            client.get("/api/v1/optimization/runs", headers=_auth(token)).status_code
            == 200
        )

    def test_the_temporary_password_stops_working(
        self, client: TestClient, admin_token: str
    ) -> None:
        account = self._new_account(client, admin_token)
        token = client.post(
            "/api/v1/auth/login",
            json={
                "email": "recien@example.test",
                "password": account["temporary_password"],
            },
        ).json()["access_token"]
        client.post(
            "/api/v1/auth/change-password",
            headers=_auth(token),
            json={
                "current_password": account["temporary_password"],
                "new_password": "una-contrasena-nueva-y-larga",
            },
        )

        assert (
            client.post(
                "/api/v1/auth/login",
                json={
                    "email": "recien@example.test",
                    "password": account["temporary_password"],
                },
            ).status_code
            == 401
        )
