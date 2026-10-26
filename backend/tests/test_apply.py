"""SESSION 34: applying an approved change.

The last place the platform's story stopped short of what it promises. A
change request could be proposed, measured, routed to the right people and
approved — and the timetable never moved. Escalated since SESSION 12, because
the question is not how to write the rows: it is **who may apply, and when.**
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from academic_ai.application.apply import ApplyError, apply_change_request
from academic_ai.domain.enums import (
    ChangeRequestStatus,
    ScenarioStatus,
    ScheduleVersionStatus,
    UserRole,
)
from academic_ai.infrastructure.db.models import (
    AuditLog,
    ChangeRequest,
    Scenario,
    ScenarioPerturbation,
    ScheduleAssignment,
    ScheduleVersion,
    User,
)
from academic_ai.security.rbac import AuthorizationError, Principal


@pytest.fixture()
def api_client(session: Session):
    """The app, wired to this test's session."""
    from fastapi.testclient import TestClient

    from academic_ai.api.app import create_app
    from academic_ai.api.deps import get_session, get_settings
    from academic_ai.security.settings import SecuritySettings

    settings = SecuritySettings(
        secret_key="t" * 64,
        environment="test",
        allowed_hosts=(),
        cors_origins=(),
        access_token_minutes=30,
        refresh_token_days=7,
    )
    app = create_app(settings)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client


@pytest.fixture()
def api_token(session: Session, api_client) -> str:
    from academic_ai.infrastructure.db.models import UserCredential
    from academic_ai.security.passwords import hash_password

    user = User(
        email=f"lister-{uuid.uuid4().hex[:6]}@example.test",
        full_name="Directora",
        primary_role=UserRole.DIRECTOR,
    )
    session.add(user)
    session.flush()
    session.add(
        UserCredential(
            app_user_id=user.id, password_hash=hash_password("contrasena-de-prueba")
        )
    )
    session.flush()
    response = api_client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "contrasena-de-prueba"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


@pytest.fixture()
def publisher(session: Session) -> Principal:
    user = User(
        email=f"pub-{uuid.uuid4().hex[:6]}@example.test",
        full_name="Publicadora",
        primary_role=UserRole.DIRECTOR,  # holds PUBLISH_SCHEDULE
    )
    session.add(user)
    session.flush()
    return Principal(user_id=str(user.id), role=UserRole.DIRECTOR)


def _build_approved(session, offered, *, block_target: bool = False):
    """A published timetable, and an APPROVED request that moves one class.

    ``block_target`` puts a second class in the room the move targets, before
    publication — it has to be before, because the assignments of a published
    version are immutable, which is the rule that makes a published timetable
    worth trusting.
    """
    version = offered.version
    slots = offered.timeslots
    session.add(
        ScheduleAssignment(
            schedule_version_id=version.id,
            class_session_id=offered.class_session.id,
            academic_period_id=offered.period.id,
            start_timeslot_id=slots[0].id,
            day_of_week=slots[0].day_of_week,
            start_slot_index=slots[0].day_slot_index,
            duration_slots=offered.class_session.duration_slots,
            requires_room=True,
            room_id=offered.room.id,
        )
    )
    if block_target:
        from academic_ai.domain.enums import Modality, SessionType
        from academic_ai.infrastructure.db.models import ClassSession

        blocker = ClassSession(
            section_id=offered.section.id,
            academic_period_id=offered.period.id,
            sequence_number=2,
            session_type=SessionType.LECTURE,
            delivery_mode=Modality.ONSITE,
            duration_slots=1,
            requires_timeslot=True,
            requires_room=True,
        )
        session.add(blocker)
        session.flush()
        session.add(
            ScheduleAssignment(
                schedule_version_id=version.id,
                class_session_id=blocker.id,
                academic_period_id=offered.period.id,
                start_timeslot_id=slots[2].id,
                day_of_week=slots[2].day_of_week,
                start_slot_index=slots[2].day_slot_index,
                duration_slots=1,
                requires_room=True,
                room_id=offered.room.id,
            )
        )
        session.flush()

    session.flush()
    version.status = ScheduleVersionStatus.PUBLISHED
    version.published_at = dt.datetime.now(dt.timezone.utc)
    version.published_by_user_id = offered.admin.id
    session.flush()

    scenario = Scenario(
        base_schedule_version_id=version.id,
        code=f"SC-{uuid.uuid4().hex[:6]}",
        name="Mover una clase",
        status=ScenarioStatus.OPEN,
    )
    session.add(scenario)
    session.flush()
    session.add(
        ScenarioPerturbation(
            scenario_id=scenario.id,
            sequence_number=1,
            kind="MOVE_SESSION",
            class_session_id=offered.class_session.id,
            timeslot_id=slots[2].id,
            payload={"start_slot_id": str(slots[2].id)},
        )
    )
    # A request cannot exist without a chain: the schema refuses one with no
    # `approval_workflow_id`, which is right — a request nobody has to
    # approve is not a request.
    from academic_ai.domain.enums import ScheduleScope
    from academic_ai.infrastructure.db.models import ApprovalStep, ApprovalWorkflow

    workflow = ApprovalWorkflow(
        institution_id=offered.institution.id,
        code=f"W-{uuid.uuid4().hex[:6]}",
        name="Cadena de prueba",
        applies_to_scope=ScheduleScope.PROGRAM,
        is_active=True,
    )
    session.add(workflow)
    session.flush()
    session.add(
        ApprovalStep(
            approval_workflow_id=workflow.id,
            step_number=1,
            required_role=UserRole.COORDINATOR,
            requires_all_affected=False,
        )
    )
    session.flush()

    request = ChangeRequest(
        academic_period_id=offered.period.id,
        approval_workflow_id=workflow.id,
        code=f"CR-{uuid.uuid4().hex[:6]}",
        title="Mover una clase",
        requested_by_user_id=offered.admin.id,
        base_schedule_version_id=version.id,
        scenario_id=scenario.id,
        status=ChangeRequestStatus.APPROVED,
        # Not `{}`: `ck_change_request_submitted_request_carries_an_impact_
        # analysis` refuses one, and rightly — a request that reached
        # approvers with no measured impact is one they approved blind.
        impact={
            "affected": {
                "students": 3,
                "teachers": 1,
                "programs": 1,
                "faculties": 1,
                "rooms": 1,
                "sessions": 1,
            },
            "new_conflicts": 0,
            "resolved_conflicts": 1,
            "score_delta": [0, 0, -1],
            "tier_codes": ["HARD", "MEDIUM", "SOFT_QUALITY"],
            "stability_delta": 1,
            "summary": "Mueve una clase",
            "scope": "PROGRAM",
            "affected_program_ids": [],
        },
    )
    session.add(request)
    session.flush()
    offered.__dict__.update(request=request, scenario=scenario, target=slots[2])
    return offered


@pytest.fixture()
def approved(session: Session, offered, publisher):
    return _build_approved(session, offered)


@pytest.fixture()
def approved_with_a_collision(session: Session, offered, publisher):
    """The same, with something already in the room the move targets."""
    return _build_approved(session, offered, block_target=True)


class TestApplying:
    def test_it_publishes_a_new_version_with_the_move(
        self, session: Session, approved, publisher
    ) -> None:
        applied = apply_change_request(session, publisher, approved.request.id)

        assert applied.moved_sessions == 1
        fresh = session.get(ScheduleVersion, uuid.UUID(applied.schedule_version_id))
        assert fresh.status is ScheduleVersionStatus.PUBLISHED
        assert fresh.version_number > approved.version.version_number
        assert fresh.parent_version_id == approved.version.id

        placed = session.scalars(
            select(ScheduleAssignment).where(
                ScheduleAssignment.schedule_version_id == fresh.id
            )
        ).all()
        assert len(placed) == 1
        assert placed[0].start_timeslot_id == approved.target.id

    def test_the_old_timetable_is_superseded_not_destroyed(
        self, session: Session, approved, publisher
    ) -> None:
        # What students read yesterday stays readable, and `parent_version_id`
        # says what it became.
        apply_change_request(session, publisher, approved.request.id)
        session.refresh(approved.version)

        assert approved.version.status is ScheduleVersionStatus.SUPERSEDED
        assert session.scalar(
            select(func.count())
            .select_from(ScheduleAssignment)
            .where(ScheduleAssignment.schedule_version_id == approved.version.id)
        ) == 1

    def test_exactly_one_version_stays_published(
        self, session: Session, approved, publisher
    ) -> None:
        # The schema allows one per period; the swap has to happen in the
        # right order or the insert is refused.
        apply_change_request(session, publisher, approved.request.id)
        published = session.scalars(
            select(ScheduleVersion)
            .where(ScheduleVersion.academic_period_id == approved.period.id)
            .where(ScheduleVersion.status == ScheduleVersionStatus.PUBLISHED)
        ).all()
        assert len(published) == 1

    def test_the_request_records_what_it_became(
        self, session: Session, approved, publisher
    ) -> None:
        applied = apply_change_request(session, publisher, approved.request.id)
        session.refresh(approved.request)

        assert approved.request.status is ChangeRequestStatus.APPLIED
        assert str(approved.request.applied_schedule_version_id) == (
            applied.schedule_version_id
        )

    def test_the_scenario_is_finally_promoted(
        self, session: Session, approved, publisher
    ) -> None:
        # PROMOTED has always meant "the timetable reflects this", and until
        # now nothing could ever make it true.
        apply_change_request(session, publisher, approved.request.id)
        session.refresh(approved.scenario)
        assert approved.scenario.status is ScenarioStatus.PROMOTED

    def test_it_is_audited(self, session: Session, approved, publisher) -> None:
        apply_change_request(session, publisher, approved.request.id)
        entries = session.scalars(
            select(AuditLog).where(AuditLog.entity_type == "change_request")
        ).all()
        assert len(entries) == 1
        assert entries[0].before["schedule_version"] == str(approved.version.id)
        assert entries[0].after["moved_sessions"] == 1


class TestWhoMayApply:
    def test_approving_is_not_applying(
        self, session: Session, approved
    ) -> None:
        """An approval is permission to apply, not the application.

        A coordinator can propose and can sign a step, and cannot publish. If
        the last approval republished the timetable by itself, the act that
        students actually read would happen without anybody choosing to
        perform it.
        """
        coordinator = Principal(user_id=str(uuid.uuid4()), role=UserRole.COORDINATOR)
        with pytest.raises(AuthorizationError):
            apply_change_request(session, coordinator, approved.request.id)


class TestWhenItMayBeApplied:
    def test_an_unapproved_request_is_refused(
        self, session: Session, approved, publisher
    ) -> None:
        approved.request.status = ChangeRequestStatus.SUBMITTED
        session.flush()
        with pytest.raises(ApplyError, match="only an APPROVED"):
            apply_change_request(session, publisher, approved.request.id)

    def test_applying_twice_is_refused(
        self, session: Session, approved, publisher
    ) -> None:
        # Idempotent by refusal rather than by silence: two versions from one
        # approval would be two publications from one decision.
        applied = apply_change_request(session, publisher, approved.request.id)
        with pytest.raises(ApplyError, match="already applied"):
            apply_change_request(session, publisher, approved.request.id)
        assert applied.version_number

    def test_a_stale_approval_is_refused(
        self, session: Session, approved, publisher
    ) -> None:
        """The heart of it.

        The impact everybody approved was measured against one timetable. If
        the published version has moved on, that impact describes a timetable
        that no longer exists — the affected students, the new conflicts and
        the per-tier deltas the approvers read were all measured somewhere
        else. Applying anyway publishes a schedule nobody approved, and it
        looks exactly like one they did.
        """
        # Somebody else publishes first: a second request, applied.
        other = ScheduleVersion(
            schedule_id=approved.version.schedule_id,
            institution_id=approved.institution.id,
            academic_period_id=approved.period.id,
            version_number=approved.version.version_number + 1,
            status=ScheduleVersionStatus.DRAFT,
        )
        session.add(other)
        session.flush()
        approved.version.status = ScheduleVersionStatus.SUPERSEDED
        session.flush()
        other.status = ScheduleVersionStatus.PUBLISHED
        other.published_at = dt.datetime.now(dt.timezone.utc)
        other.published_by_user_id = approved.admin.id
        session.flush()

        with pytest.raises(ApplyError, match="no longer exists"):
            apply_change_request(session, publisher, approved.request.id)

    def test_the_refusal_says_which_versions_disagree(
        self, session: Session, approved, publisher
    ) -> None:
        # "Conflict" with no numbers leaves somebody guessing what changed.
        other = ScheduleVersion(
            schedule_id=approved.version.schedule_id,
            institution_id=approved.institution.id,
            academic_period_id=approved.period.id,
            version_number=99,
            status=ScheduleVersionStatus.DRAFT,
        )
        session.add(other)
        session.flush()
        approved.version.status = ScheduleVersionStatus.SUPERSEDED
        session.flush()
        other.status = ScheduleVersionStatus.PUBLISHED
        other.published_at = dt.datetime.now(dt.timezone.utc)
        other.published_by_user_id = approved.admin.id
        session.flush()

        with pytest.raises(ApplyError) as raised:
            apply_change_request(session, publisher, approved.request.id)
        assert "99" in str(raised.value)

    def test_a_period_with_nothing_published_is_refused(
        self, session: Session, approved, publisher
    ) -> None:
        approved.version.status = ScheduleVersionStatus.DRAFT
        session.flush()
        with pytest.raises(ApplyError, match="no published timetable"):
            apply_change_request(session, publisher, approved.request.id)

    def test_a_move_of_a_session_that_is_not_placed_is_refused(
        self, session: Session, approved, publisher
    ) -> None:
        # Dropping it silently would make the published change differ from the
        # approved one by exactly the part nobody noticed.
        session.add(
            ScenarioPerturbation(
                scenario_id=approved.scenario.id,
                sequence_number=2,
                kind="MOVE_SESSION",
                class_session_id=approved.class_session.id,
                timeslot_id=approved.timeslots[1].id,
                payload={},
            )
        )
        session.flush()
        # Two perturbations, one placed session: the second cannot be applied.
        with pytest.raises(ApplyError, match="are placed"):
            apply_change_request(session, publisher, approved.request.id)

    def test_an_unknown_request_is_refused(
        self, session: Session, publisher
    ) -> None:
        with pytest.raises(ApplyError, match="no change request"):
            apply_change_request(session, publisher, uuid.uuid4())


class TestListingRequests:
    """SESSION 35: finding a request without already knowing its id.

    `GET /change-requests/{id}` existed and nothing listed them, so the only
    way to reach a request was to have been handed its id. The list is also
    the first place row-level scope reaches past the CRUD surface — it works
    here because a request records *which* programmes it affects.
    """

    def _list(self, client, token, **params):
        response = client.get(
            "/api/v1/change-requests",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
        )
        assert response.status_code == 200, response.text
        return response.json()["requests"]

    def test_a_request_appears_in_the_list(
        self, api_client, api_token, approved
    ) -> None:
        codes = {r["code"] for r in self._list(api_client, api_token)}
        assert approved.request.code in codes

    def test_the_row_carries_enough_to_choose_from(
        self, api_client, api_token, approved
    ) -> None:
        # A list is read to decide which request to open. Numbers a person
        # scans, not the whole chain.
        row = next(
            r
            for r in self._list(api_client, api_token)
            if r["code"] == approved.request.code
        )
        assert row["status"] == "APPROVED"
        assert row["affected_students"] == 3
        assert row["scope"] == "PROGRAM"

    def test_it_can_be_filtered_by_status(
        self, api_client, api_token, approved
    ) -> None:
        assert self._list(api_client, api_token, status_filter="APPROVED")
        assert self._list(api_client, api_token, status_filter="REJECTED") == []

    def test_an_unknown_status_is_refused_rather_than_ignored(
        self, api_client, api_token
    ) -> None:
        # Silently returning everything for a typo'd filter is how somebody
        # concludes there are no rejected requests.
        response = api_client.get(
            "/api/v1/change-requests",
            headers={"Authorization": f"Bearer {api_token}"},
            params={"status_filter": "APROBADO"},
        )
        assert response.status_code == 422


class TestTheListIsNarrowedByJurisdiction:
    """The first place row-level scope reaches past the CRUD surface."""

    def _token(self, session, api_client, role, email):
        from academic_ai.infrastructure.db.models import UserCredential
        from academic_ai.security.passwords import hash_password

        user = User(email=email, full_name=email, primary_role=role)
        session.add(user)
        session.flush()
        session.add(
            UserCredential(
                app_user_id=user.id, password_hash=hash_password("contrasena-de-prueba")
            )
        )
        session.flush()
        response = api_client.post(
            "/api/v1/auth/login",
            json={"email": email, "password": "contrasena-de-prueba"},
        )
        assert response.status_code == 200, response.text
        return user, response.json()["access_token"]

    def _assign(self, session, user, program_id):
        from academic_ai.infrastructure.db.models import UserScopeAssignment

        session.add(
            UserScopeAssignment(app_user_id=user.id, program_id=program_id)
        )
        session.flush()

    def _codes(self, api_client, token):
        response = api_client.get(
            "/api/v1/change-requests",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200, response.text
        return {r["code"] for r in response.json()["requests"]}

    def test_a_coordinator_sees_a_request_that_reaches_their_programme(
        self, session: Session, api_client, approved
    ) -> None:
        approved.request.impact = {
            **approved.request.impact,
            "affected_program_ids": [str(approved.program.id)],
        }
        session.flush()

        user, token = self._token(
            session, api_client, UserRole.COORDINATOR, "mine@example.test"
        )
        self._assign(session, user, approved.program.id)
        assert approved.request.code in self._codes(api_client, token)

    def test_and_not_one_that_does_not(
        self, session: Session, api_client, approved
    ) -> None:
        approved.request.impact = {
            **approved.request.impact,
            "affected_program_ids": [str(approved.other_program.id)],
        }
        session.flush()

        user, token = self._token(
            session, api_client, UserRole.COORDINATOR, "theirs@example.test"
        )
        self._assign(session, user, approved.program.id)
        assert approved.request.code not in self._codes(api_client, token)

    def test_you_always_see_what_you_submitted(
        self, session: Session, api_client, approved
    ) -> None:
        # Without this your own request vanishes from your list the moment it
        # touches somebody else's programme — exactly when you most want to
        # follow it.
        user, token = self._token(
            session, api_client, UserRole.COORDINATOR, "author@example.test"
        )
        self._assign(session, user, approved.program.id)
        approved.request.requested_by_user_id = user.id
        approved.request.impact = {
            **approved.request.impact,
            "affected_program_ids": [str(approved.other_program.id)],
        }
        session.flush()

        assert approved.request.code in self._codes(api_client, token)

    def test_a_request_recording_no_programmes_stays_visible(
        self, session: Session, api_client, approved
    ) -> None:
        # Older than SESSION 23, or it genuinely touched none. Hiding it would
        # make the list quietly incomplete rather than narrowed.
        approved.request.impact = {
            **approved.request.impact,
            "affected_program_ids": [],
        }
        session.flush()

        user, token = self._token(
            session, api_client, UserRole.COORDINATOR, "empty@example.test"
        )
        self._assign(session, user, approved.program.id)
        assert approved.request.code in self._codes(api_client, token)

    def test_an_institution_wide_role_sees_everything(
        self, session: Session, api_client, approved
    ) -> None:
        approved.request.impact = {
            **approved.request.impact,
            "affected_program_ids": [str(approved.other_program.id)],
        }
        session.flush()

        _user, token = self._token(
            session, api_client, UserRole.ADMIN, "wide@example.test"
        )
        assert approved.request.code in self._codes(api_client, token)


class TestItRefusesAnImpossibleResult:
    """SESSION 35: the database was catching what the application should.

    A move can be legal for the session that moves and still collide with
    what is already there: `compute_domains` answers where a session *may* go
    given its own constraints, and whether another class holds that room at
    that hour is a property of the whole placement. The impact preview warns
    about it (`occupancy_clashes`) and deliberately does not block — CP-SAT is
    the feasibility authority, and a coordinator may legitimately propose
    something that needs the other class moved too.

    What must not happen is publishing it. Before this, the GiST exclusion
    constraint fired mid-write and the person applying got **"internal
    error"** for a decision that was theirs to reconsider.
    """

    def test_a_collision_is_refused_with_a_sentence(
        self, session: Session, approved_with_a_collision, publisher
    ) -> None:
        with pytest.raises(ApplyError, match="same room"):
            apply_change_request(
                session, publisher, approved_with_a_collision.request.id
            )

    def test_the_refusal_says_the_other_class_must_move_too(
        self, session: Session, approved_with_a_collision, publisher
    ) -> None:
        # "Conflict" is not actionable. What the person needs to know is that
        # their proposal was fine and something else is in the way.
        with pytest.raises(ApplyError) as raised:
            apply_change_request(
                session, publisher, approved_with_a_collision.request.id
            )
        assert "would have to move too" in str(raised.value)

    def test_nothing_is_written_when_it_is_refused(
        self, session: Session, approved_with_a_collision, publisher
    ) -> None:
        # The reason the check runs before the write rather than during it:
        # a half-built version left behind by a failed apply is worse than
        # the refusal.
        before = session.scalar(
            select(func.count()).select_from(ScheduleVersion)
        )

        with pytest.raises(ApplyError):
            apply_change_request(
                session, publisher, approved_with_a_collision.request.id
            )

        # No rollback: the point is that nothing was written in the first
        # place. Rolling back would also undo the fixture and prove nothing.
        assert (
            session.scalar(select(func.count()).select_from(ScheduleVersion))
            == before
        )

    def test_a_move_with_no_collision_still_applies(
        self, session: Session, approved, publisher
    ) -> None:
        applied = apply_change_request(session, publisher, approved.request.id)
        assert applied.moved_sessions == 1
