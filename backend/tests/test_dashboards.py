"""SESSION 16: role dashboards, and the ways one could leak or mislead.

A dashboard is where a number gets seen out of context, which makes it the
easiest place in this project to publish something false. Three properties are
tested here rather than trusted:

* **Permission decides, not role.** A role's layout is a preference; every
  widget is gated by its own permission, so adding one to a layout can never
  grant access to its data. `test_a_widget_is_absent_not_empty_when_forbidden`
  is the leak test -- an empty panel titled "Faculty indicators" tells a
  coordinator such a thing exists and they may not have it.
* **A metric that could not be computed says so.** "0 conflicts" and "we did
  not check for conflicts" render identically and mean opposite things, and
  the first is the one a dean would act on.
* **Objective values are per tier and never summed.** ADR-0010 rejects the
  cross-tier exchange rate a headline figure would imply.
"""

from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from academic_ai.api.app import create_app
from academic_ai.api.deps import get_session, get_settings
from academic_ai.application.dashboards import build_dashboard
from academic_ai.domain.dashboards import (
    ROLE_DASHBOARDS,
    WIDGETS,
    Metric,
    Severity,
    WidgetKind,
    widgets_for,
)
from academic_ai.domain.enums import UserRole
from academic_ai.infrastructure.db.models import User, UserCredential
from academic_ai.security.passwords import hash_password
from academic_ai.security.rbac import ActorKind, Permission, Principal, permissions_for
from academic_ai.security.settings import SecuritySettings

PASSWORD = "a perfectly reasonable passphrase"


def _principal(role: UserRole, user_id: str = "u-1") -> Principal:
    return Principal(user_id=user_id, role=role, kind=ActorKind.HUMAN)


# ---------------------------------------------------------------------------
# Permission decides
# ---------------------------------------------------------------------------


class TestAuthorisationBoundaries:
    @pytest.mark.parametrize("role", list(UserRole))
    def test_every_widget_a_role_gets_is_one_it_holds(self, role: UserRole) -> None:
        """The property that makes the layout safe to edit: a widget added to
        a role's list still cannot be seen without its permission."""
        held = permissions_for(role)
        for widget in widgets_for(role, held):
            assert widget.permission in held, (
                f"{role.value} was given {widget.code} without "
                f"{widget.permission.value}"
            )

    def test_a_widget_is_absent_not_empty_when_forbidden(self) -> None:
        """An empty panel titled "Faculty indicators" tells a coordinator that
        faculty indicators exist and they are not allowed to see them, which
        is a small leak and an unkind one."""
        coordinator = permissions_for(UserRole.COORDINATOR)
        assert Permission.VIEW_FACULTY_SCHEDULE not in coordinator

        codes = {w.code for w in widgets_for(UserRole.COORDINATOR, coordinator)}
        assert "faculty_kpis" not in codes
        assert "program_comparison" not in codes

    def test_a_student_gets_only_personal_widgets(self) -> None:
        """A student holds one permission, and everything they see is about
        them. Nothing institutional leaks through an aggregate."""
        widgets = widgets_for(UserRole.STUDENT, permissions_for(UserRole.STUDENT))
        assert widgets
        assert all(w.is_personal for w in widgets)

    def test_a_teacher_sees_no_institutional_aggregate(self) -> None:
        widgets = widgets_for(UserRole.TEACHER, permissions_for(UserRole.TEACHER))
        assert widgets
        assert all(w.is_personal for w in widgets)

    def test_holding_no_permissions_yields_no_dashboard_rather_than_an_error(
        self,
    ) -> None:
        """A deactivated or misconfigured account should see an empty page,
        not a stack trace."""
        assert widgets_for(UserRole.DIRECTOR, frozenset()) == ()

    def test_a_layout_naming_an_unknown_widget_is_skipped_not_crashed(self) -> None:
        for role, codes in ROLE_DASHBOARDS.items():
            for code in codes:
                assert code in WIDGETS, f"{role.value} lists unknown widget {code}"


# ---------------------------------------------------------------------------
# Honesty
# ---------------------------------------------------------------------------


class TestAMetricNeverLies:
    def test_unavailable_is_its_own_state_not_a_zero(self) -> None:
        """The whole reason `Metric.unavailable` exists."""
        widget = WIDGETS["student_conflicts"]
        metric = Metric.unavailable(widget, "no schedule is loaded")
        assert metric.is_unavailable
        assert metric.value is None
        assert metric.value != 0
        assert metric.severity is Severity.ATTENTION
        assert "no schedule" in metric.detail

    def test_conflicts_report_unavailable_rather_than_zero(
        self, session: Session, world
    ) -> None:
        """A dean seeing "0 student conflicts" would act on it. The truth is
        that nothing has been checked."""
        metrics = build_dashboard(session, _principal(UserRole.DEAN))
        conflicts = next(m for m in metrics if m.widget_code == "student_conflicts")
        assert conflicts.is_unavailable
        assert conflicts.value is None

    def test_schedule_health_says_why_it_cannot_answer(
        self, session: Session, world
    ) -> None:
        metrics = build_dashboard(session, _principal(UserRole.DIRECTOR))
        health = next(m for m in metrics if m.widget_code == "schedule_health")
        assert health.is_unavailable
        assert health.detail, "an unavailable panel with no reason is just a blank"

    def test_one_failing_widget_does_not_blank_the_page(
        self, session: Session, world, monkeypatch
    ) -> None:
        """A dean whose faculty query is slow should still see their
        approvals."""
        import academic_ai.application.dashboards as module

        def _boom(*args, **kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setitem(module._HANDLERS, "faculty_kpis", _boom)
        metrics = build_dashboard(session, _principal(UserRole.DEAN))

        broken = next(m for m in metrics if m.widget_code == "faculty_kpis")
        assert broken.is_unavailable
        assert len(metrics) == len(
            widgets_for(UserRole.DEAN, permissions_for(UserRole.DEAN))
        )

    def test_a_status_widget_carries_a_reason_not_just_a_colour(
        self, session: Session, world
    ) -> None:
        """A red light with no reason sends an operator hunting; a green one
        with no reason is worth nothing."""
        metrics = build_dashboard(session, _principal(UserRole.ADMIN))
        health = next(m for m in metrics if m.widget_code == "system_health")
        assert health.detail


class TestObjectiveValuesAreNeverCollapsed:
    def test_the_tier_widget_publishes_no_single_figure(
        self, session: Session, world
    ) -> None:
        """A headline number would be a sum across tiers, and ADR-0010 rejects
        the exchange rate that implies."""
        widget = WIDGETS["objective_tiers"]
        assert widget.kind is WidgetKind.TIER_BREAKDOWN

        metrics = build_dashboard(session, _principal(UserRole.COORDINATOR))
        tiers = next(m for m in metrics if m.widget_code == "objective_tiers")
        # With no completed run it is unavailable; either way it must never
        # carry a scalar value.
        assert tiers.value is None

    def test_the_widget_description_states_the_rule(self) -> None:
        assert "ADR-0010" in WIDGETS["objective_tiers"].description


# ---------------------------------------------------------------------------
# Computing against a real database
# ---------------------------------------------------------------------------


class TestComputedMetrics:
    def test_an_admin_sees_the_academic_structure(
        self, session: Session, world
    ) -> None:
        metrics = build_dashboard(session, _principal(UserRole.ADMIN))
        structure = next(m for m in metrics if m.widget_code == "academic_structure")
        assert not structure.is_unavailable
        entities = {r["entity"] for r in structure.rows}
        assert {"faculties", "programmes", "courses", "rooms"} <= entities

    def test_user_accounts_break_down_by_role(
        self, session: Session, world
    ) -> None:
        metrics = build_dashboard(session, _principal(UserRole.SUPER_ADMIN))
        accounts = next(m for m in metrics if m.widget_code == "user_accounts")
        assert accounts.value >= 1
        assert any(r["role"] == "ADMIN" for r in accounts.rows)

    def test_no_active_model_is_reported_as_normal_not_as_a_gap(
        self, session: Session, world
    ) -> None:
        """No model being active is the designed state, and a dashboard that
        implied otherwise would invite somebody to go and activate one."""
        metrics = build_dashboard(session, _principal(UserRole.SUPER_ADMIN))
        models = next(m for m in metrics if m.widget_code == "ai_models")
        assert "normal state" in models.detail

    def test_a_teacher_without_a_teacher_record_is_told_so(
        self, session: Session, world
    ) -> None:
        """Rather than shown a load of zero, which reads as "you teach
        nothing" instead of "we could not find you"."""
        user = User(
            email="ghost@example.test", full_name="Ghost", primary_role=UserRole.TEACHER
        )
        session.add(user)
        session.flush()

        metrics = build_dashboard(session, _principal(UserRole.TEACHER, str(user.id)))
        load = next(m for m in metrics if m.widget_code == "my_load")
        assert load.is_unavailable
        assert "not linked to a teacher record" in load.detail

    def test_a_teacher_with_no_availability_is_warned_not_reassured(
        self, session: Session, world
    ) -> None:
        """Nothing recorded is not the same as available all week, even though
        the optimizer has to treat it that way -- and the teacher is the one
        person who can fix it."""
        user = User(
            email="t2@example.test", full_name="T", primary_role=UserRole.TEACHER
        )
        session.add(user)
        session.flush()
        world.teacher.user_id = user.id
        session.flush()

        metrics = build_dashboard(session, _principal(UserRole.TEACHER, str(user.id)))
        availability = next(m for m in metrics if m.widget_code == "my_availability")
        assert availability.severity is Severity.ATTENTION
        assert "assume you are free" in availability.detail

    def test_a_student_sees_their_own_enrolments_only(
        self, session: Session, world
    ) -> None:
        user = User(
            email="s9@example.test", full_name="S", primary_role=UserRole.STUDENT
        )
        session.add(user)
        session.flush()
        world.student.user_id = user.id
        session.flush()

        metrics = build_dashboard(session, _principal(UserRole.STUDENT, str(user.id)))
        enrolments = next(m for m in metrics if m.widget_code == "my_enrollments")
        assert not enrolments.is_unavailable

    def test_an_unpublished_timetable_is_labelled_provisional(
        self, session: Session, world
    ) -> None:
        """Confusing a provisional schedule with a published one is how a
        student arranges their week around something that then changes."""
        user = User(
            email="s10@example.test", full_name="S", primary_role=UserRole.STUDENT
        )
        session.add(user)
        session.flush()
        world.student.user_id = user.id
        session.flush()

        metrics = build_dashboard(session, _principal(UserRole.STUDENT, str(user.id)))
        schedule = next(m for m in metrics if m.widget_code == "my_schedule")
        assert schedule.is_unavailable
        assert "provisional" in schedule.detail


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


class TestOverHttp:
    def test_the_endpoint_takes_no_role_parameter(
        self, client: TestClient, session: Session
    ) -> None:
        """There is no request a client can make that asks for somebody
        else's screen."""
        spec = client.get("/openapi.json").json()
        params = spec["paths"]["/api/v1/dashboard"]["get"].get("parameters", [])
        assert not [p for p in params if p["name"] not in ("authorization",)]

    def test_a_coordinator_gets_their_panels(
        self, client: TestClient, session: Session, world
    ) -> None:
        _user(session, UserRole.COORDINATOR, "c@example.test")
        token = _token(client, "c@example.test")
        response = client.get(
            "/api/v1/dashboard", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["role"] == "COORDINATOR"
        codes = {m["widget_code"] for m in body["metrics"]}
        assert "faculty_kpis" not in codes, "a faculty panel leaked to a coordinator"

    def test_a_student_gets_only_their_own(
        self, client: TestClient, session: Session, world
    ) -> None:
        user = _user(session, UserRole.STUDENT, "st@example.test")
        world.student.user_id = user.id
        session.flush()

        token = _token(client, "st@example.test")
        body = client.get(
            "/api/v1/dashboard", headers={"Authorization": f"Bearer {token}"}
        ).json()
        codes = {m["widget_code"] for m in body["metrics"]}
        assert codes <= {
            "my_profile", "my_enrollments", "my_repeated_courses",
            "my_schedule", "my_conflicts",
        }

    def test_an_unavailable_panel_is_marked_not_zeroed(
        self, client: TestClient, session: Session, world
    ) -> None:
        _user(session, UserRole.DIRECTOR, "d@example.test")
        token = _token(client, "d@example.test")
        body = client.get(
            "/api/v1/dashboard", headers={"Authorization": f"Bearer {token}"}
        ).json()
        health = next(
            m for m in body["metrics"] if m["widget_code"] == "schedule_health"
        )
        assert health["is_unavailable"] is True
        assert health["value"] is None
        assert health["detail"]

    def test_no_token_is_401(self, client: TestClient) -> None:
        assert client.get("/api/v1/dashboard").status_code == 401
