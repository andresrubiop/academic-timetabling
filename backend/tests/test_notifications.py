"""SESSION 17: notifications, and the noise they are designed not to make.

A notification system's failure mode is not silence, it is noise. A
coordinator who receives forty a day stops reading them, at which point the
one that mattered is lost exactly as completely as if it had never been sent.
So most of this file is about what does **not** get sent:

* nobody is told what they just did;
* forty moved sessions reach a student as one message, saying how many of
  *their* classes moved;
* the third approver is not told a week early about a request that has not
  reached them.

The other half is the channel abstraction. EMAIL and PUSH are declared and
unimplemented, and they **refuse** rather than quietly succeeding -- a channel
that returned success and did nothing would let somebody configure email, see
no errors, and believe deans had been told about approvals for weeks.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.api.app import create_app
from academic_ai.api.deps import get_session, get_settings
from academic_ai.application.notifications import (
    ChannelUnavailable,
    EmailChannel,
    PushChannel,
    WebChannel,
    channel_for,
    dispatch,
    mark_read,
    unread_count,
)
from academic_ai.domain.enums import (
    NotificationChannel,
    NotificationKind,
    UserRole,
)
from academic_ai.domain.notifications import (
    ACTIONABLE_KINDS,
    KIND_RULES,
    NotificationRequest,
    coalesce,
    rule_for,
    suppress_actor,
)
from academic_ai.infrastructure.db.models import Notification, User, UserCredential
from academic_ai.security.passwords import hash_password
from academic_ai.security.settings import SecuritySettings

PASSWORD = "a perfectly reasonable passphrase"
_K = NotificationKind


def _request(kind: _K, recipient: str = "u-1", **kwargs) -> NotificationRequest:
    defaults = {"title": "Something happened"}
    defaults.update(kwargs)
    return NotificationRequest(kind=kind, recipient_user_id=recipient, **defaults)


# ---------------------------------------------------------------------------
# What does not get sent
# ---------------------------------------------------------------------------


class TestNobodyIsToldWhatTheyJustDid:
    def test_the_actor_is_suppressed(self) -> None:
        """The largest source of noise in a naive implementation: the actor is
        almost always inside the set of interested parties. The director who
        approves is in the approval chain."""
        kept = suppress_actor(
            [_request(_K.REQUEST_APPROVED, recipient="dir-1", actor_user_id="dir-1")]
        )
        assert kept == ()

    def test_everybody_else_still_hears_about_it(self) -> None:
        kept = suppress_actor(
            [
                _request(_K.REQUEST_APPROVED, recipient="dir-1", actor_user_id="dir-1"),
                _request(_K.REQUEST_APPROVED, recipient="coord-1", actor_user_id="dir-1"),
            ]
        )
        assert [r.recipient_user_id for r in kept] == ["coord-1"]

    def test_an_event_with_no_actor_reaches_everybody(self) -> None:
        kept = suppress_actor([_request(_K.PUBLISHED_SCHEDULE, recipient="s-1")])
        assert len(kept) == 1

    def test_a_finished_run_does_notify_the_person_who_started_it(self) -> None:
        """The deliberate exception: they submitted it and went away, which is
        the entire reason the run is asynchronous."""
        kept = suppress_actor(
            [
                _request(
                    _K.OPTIMIZATION_COMPLETE, recipient="c-1", actor_user_id="c-1"
                )
            ]
        )
        assert len(kept) == 1
        assert rule_for(_K.OPTIMIZATION_COMPLETE).notify_actor


class TestOneEventOneMessage:
    def test_forty_moved_sessions_become_one_notification(self) -> None:
        """A student with three affected classes has one problem, not three --
        and certainly not forty."""
        requests = [
            _request(
                _K.SCHEDULE_CHANGED,
                recipient="s-1",
                subject_type="schedule_version",
                subject_id="v-1",
                payload={"sessions": [f"S{i}"]},
            )
            for i in range(40)
        ]
        collapsed = coalesce(requests)
        assert len(collapsed) == 1
        assert collapsed[0].payload["occurrences"] == 40

    def test_the_collapsed_message_names_what_it_collapsed(self) -> None:
        """"3 of your classes moved" is only useful if it can say which
        three."""
        requests = [
            _request(
                _K.SCHEDULE_CHANGED, recipient="s-1",
                subject_type="v", subject_id="1",
                payload={"sessions": [name]},
            )
            for name in ("MAT101", "FIS201", "QUI101")
        ]
        collapsed = coalesce(requests)
        assert set(collapsed[0].payload["sessions"]) == {"MAT101", "FIS201", "QUI101"}

    def test_two_people_are_not_collapsed_into_one(self) -> None:
        collapsed = coalesce(
            [
                _request(_K.SCHEDULE_CHANGED, "s-1", subject_type="v", subject_id="1"),
                _request(_K.SCHEDULE_CHANGED, "s-2", subject_type="v", subject_id="1"),
            ]
        )
        assert {r.recipient_user_id for r in collapsed} == {"s-1", "s-2"}

    def test_two_different_subjects_are_not_collapsed(self) -> None:
        collapsed = coalesce(
            [
                _request(_K.SCHEDULE_CHANGED, "s-1", subject_type="v", subject_id="1"),
                _request(_K.SCHEDULE_CHANGED, "s-1", subject_type="v", subject_id="2"),
            ]
        )
        assert len(collapsed) == 2

    def test_two_approvals_waiting_on_one_person_stay_two_things_to_do(
        self,
    ) -> None:
        """Non-coalescing on purpose: two different change requests are two
        decisions, not one."""
        assert not rule_for(_K.APPROVAL_REQUIRED).coalesces
        collapsed = coalesce(
            [
                _request(_K.APPROVAL_REQUIRED, "d-1", subject_id="cr-1"),
                _request(_K.APPROVAL_REQUIRED, "d-1", subject_id="cr-2"),
            ]
        )
        assert len(collapsed) == 2


class TestTheRulesAreComplete:
    def test_every_kind_the_playbook_names_has_a_rule(self) -> None:
        assert set(KIND_RULES) == set(NotificationKind)

    def test_a_provisional_schedule_is_a_different_kind_from_a_published_one(
        self,
    ) -> None:
        """A student told once that their timetable is ready should not have
        to guess whether this one is the real one."""
        assert _K.PROVISIONAL_SCHEDULE in KIND_RULES
        assert _K.PUBLISHED_SCHEDULE in KIND_RULES
        assert _K.PROVISIONAL_SCHEDULE is not _K.PUBLISHED_SCHEDULE

    def test_a_rejection_is_actionable_but_an_approval_is_not(self) -> None:
        """A rejection carries a reason and the requester is the one who can
        answer it. An approval needs nothing from anybody."""
        assert _K.REQUEST_REJECTED in ACTIONABLE_KINDS
        assert _K.REQUEST_APPROVED not in ACTIONABLE_KINDS

    def test_an_ai_recommendation_is_actionable_because_it_never_self_applies(
        self,
    ) -> None:
        assert _K.AI_RECOMMENDATION in ACTIONABLE_KINDS


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class TestChannels:
    def test_web_is_available_and_the_others_are_not(self) -> None:
        assert channel_for(NotificationChannel.WEB).is_available
        assert not channel_for(NotificationChannel.EMAIL).is_available
        assert not channel_for(NotificationChannel.PUSH).is_available

    @pytest.mark.parametrize("port", [EmailChannel(), PushChannel()])
    def test_an_unimplemented_channel_refuses_rather_than_dropping(
        self, port, session: Session
    ) -> None:
        """A `deliver()` that returned success and did nothing would let
        somebody configure email, see no errors, and believe deans had been
        told about approvals for weeks."""
        with pytest.raises(ChannelUnavailable):
            port.deliver(session, _request(_K.APPROVAL_REQUIRED))

    def test_an_undeliverable_notification_is_reported_not_hidden(
        self, session: Session, world
    ) -> None:
        report = dispatch(
            session,
            [
                _request(
                    _K.APPROVAL_REQUIRED,
                    recipient=str(world.admin.id),
                    channel=NotificationChannel.EMAIL,
                )
            ],
        )
        assert report.delivered == 0
        assert report.has_failures
        assert "not configured" in report.failures[0]

    def test_delivery_failure_does_not_raise(
        self, session: Session, world
    ) -> None:
        """An optimization run that finished must not be recorded as failed
        because a notification could not be written."""
        report = dispatch(
            session,
            [
                _request(
                    _K.OPTIMIZATION_COMPLETE,
                    recipient=str(world.admin.id),
                    channel=NotificationChannel.PUSH,
                )
            ],
        )
        assert report.requested == 1
        assert report.delivered == 0


# ---------------------------------------------------------------------------
# Dispatching against a real database
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_a_web_notification_is_written_and_readable(
        self, session: Session, world
    ) -> None:
        report = dispatch(
            session,
            [
                _request(
                    _K.PUBLISHED_SCHEDULE,
                    recipient=str(world.admin.id),
                    title="El horario está publicado",
                    body="Versión 1",
                )
            ],
        )
        assert report.delivered == 1

        row = session.scalars(
            select(Notification).where(
                Notification.recipient_user_id == world.admin.id
            )
        ).one()
        assert row.title == "El horario está publicado"
        assert row.read_at is None

    def test_actionability_comes_from_the_rule_not_the_caller(
        self, session: Session, world
    ) -> None:
        """So a caller cannot mark routine news actionable and jump the
        queue."""
        dispatch(
            session,
            [
                _request(_K.APPROVAL_REQUIRED, recipient=str(world.admin.id)),
                _request(_K.PUBLISHED_SCHEDULE, recipient=str(world.admin.id)),
            ],
        )
        rows = {
            n.kind: n.is_actionable
            for n in session.scalars(select(Notification)).all()
        }
        assert rows[_K.APPROVAL_REQUIRED] is True
        assert rows[_K.PUBLISHED_SCHEDULE] is False

    def test_the_report_counts_suppressions_separately_from_failures(
        self, session: Session, world
    ) -> None:
        """A suppression is the rules working; a failure is not."""
        actor = str(world.admin.id)
        report = dispatch(
            session,
            [
                _request(_K.REQUEST_APPROVED, recipient=actor, actor_user_id=actor),
                _request(_K.REQUEST_APPROVED, recipient=actor),
            ],
        )
        assert report.suppressed == 1
        assert report.delivered == 1
        assert not report.has_failures

    def test_unread_count_and_mark_read(
        self, session: Session, world
    ) -> None:
        user_id = str(world.admin.id)
        dispatch(
            session,
            [
                _request(_K.PUBLISHED_SCHEDULE, recipient=user_id),
                _request(_K.OPTIMIZATION_COMPLETE, recipient=user_id),
            ],
        )
        assert unread_count(session, user_id) == 2

        marked = mark_read(session, user_id)
        assert marked == 2
        assert unread_count(session, user_id) == 0

    def test_marking_read_twice_marks_nothing_the_second_time(
        self, session: Session, world
    ) -> None:
        user_id = str(world.admin.id)
        dispatch(session, [_request(_K.PUBLISHED_SCHEDULE, recipient=user_id)])
        mark_read(session, user_id)
        assert mark_read(session, user_id) == 0

    def test_one_person_cannot_mark_anothers_notification_read(
        self, session: Session, world
    ) -> None:
        """Scoped by the query rather than checked afterwards, so somebody
        else's id simply matches nothing."""
        other = User(
            email="other@example.test", full_name="O", primary_role=UserRole.TEACHER
        )
        session.add(other)
        session.flush()

        dispatch(session, [_request(_K.PUBLISHED_SCHEDULE, recipient=str(other.id))])
        theirs = session.scalars(select(Notification)).one()

        marked = mark_read(session, str(world.admin.id), [str(theirs.id)])
        assert marked == 0
        session.refresh(theirs)
        assert theirs.read_at is None


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings() -> SecuritySettings:
    return SecuritySettings(
        secret_key="t" * 64, environment="test", allowed_hosts=(), cors_origins=(),
        access_token_minutes=30, refresh_token_days=7,
    )


@pytest.fixture()
def client(session: Session, settings: SecuritySettings):
    app = create_app(settings)
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c


def _user(session: Session, role: UserRole, email: str) -> User:
    user = User(email=email, full_name="U", primary_role=role)
    session.add(user)
    session.flush()
    session.add(
        UserCredential(app_user_id=user.id, password_hash=hash_password(PASSWORD))
    )
    session.flush()
    return user


def _token(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


class TestOverHttp:
    def test_you_see_only_your_own(
        self, client: TestClient, session: Session
    ) -> None:
        mine = _user(session, UserRole.TEACHER, "mine@example.test")
        theirs = _user(session, UserRole.TEACHER, "theirs@example.test")
        dispatch(
            session,
            [
                _request(_K.PUBLISHED_SCHEDULE, recipient=str(mine.id), title="Mine"),
                _request(_K.PUBLISHED_SCHEDULE, recipient=str(theirs.id), title="Theirs"),
            ],
        )

        token = _token(client, "mine@example.test")
        body = client.get("/api/v1/notifications", headers=_auth(token)).json()
        titles = {n["title"] for n in body["items"]}
        assert titles == {"Mine"}
        assert body["total"] == 1

    def test_actionable_notifications_come_first(
        self, client: TestClient, session: Session
    ) -> None:
        """Sorting purely by recency buries the thing somebody is blocked on
        under an hour of routine completions."""
        user = _user(session, UserRole.DIRECTOR, "d@example.test")
        dispatch(session, [_request(_K.APPROVAL_REQUIRED, recipient=str(user.id))])
        for _ in range(3):
            dispatch(
                session,
                [_request(_K.OPTIMIZATION_COMPLETE, recipient=str(user.id))],
            )

        token = _token(client, "d@example.test")
        items = client.get("/api/v1/notifications", headers=_auth(token)).json()["items"]
        assert items[0]["kind"] == "APPROVAL_REQUIRED"

    def test_the_badge_counts_actionable_separately(
        self, client: TestClient, session: Session
    ) -> None:
        """Counting everything unread makes the badge a permanent
        decoration."""
        user = _user(session, UserRole.DIRECTOR, "d2@example.test")
        dispatch(
            session,
            [
                _request(_K.APPROVAL_REQUIRED, recipient=str(user.id)),
                _request(_K.PUBLISHED_SCHEDULE, recipient=str(user.id)),
            ],
        )
        token = _token(client, "d2@example.test")
        body = client.get("/api/v1/notifications", headers=_auth(token)).json()
        assert body["unread"] == 2
        assert body["unread_actionable"] == 1

    def test_marking_read_updates_the_count(
        self, client: TestClient, session: Session
    ) -> None:
        user = _user(session, UserRole.TEACHER, "t@example.test")
        dispatch(session, [_request(_K.PUBLISHED_SCHEDULE, recipient=str(user.id))])
        token = _token(client, "t@example.test")

        response = client.post(
            "/api/v1/notifications/read", json={}, headers=_auth(token)
        )
        assert response.status_code == 200
        assert response.json() == {"marked": 1, "unread": 0}

    def test_the_available_channels_are_published(
        self, client: TestClient, session: Session
    ) -> None:
        """So nobody has to infer from silence whether email is going out."""
        _user(session, UserRole.TEACHER, "ch@example.test")
        token = _token(client, "ch@example.test")
        body = client.get(
            "/api/v1/notifications/channels", headers=_auth(token)
        ).json()
        by_channel = {c["channel"]: c for c in body}
        assert by_channel["WEB"]["is_available"] is True
        assert by_channel["EMAIL"]["is_available"] is False
        assert "not configured" in by_channel["EMAIL"]["detail"]

    def test_there_is_no_user_parameter_to_ask_for_somebody_else(
        self, client: TestClient
    ) -> None:
        spec = client.get("/openapi.json").json()
        params = spec["paths"]["/api/v1/notifications"]["get"].get("parameters", [])
        names = {p["name"] for p in params}
        assert "user_id" not in names
        assert "recipient_user_id" not in names

    def test_no_token_is_401(self, client: TestClient) -> None:
        assert client.get("/api/v1/notifications").status_code == 401
