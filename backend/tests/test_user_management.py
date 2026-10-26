"""SESSION 33: accounts, and the password nobody could change.

Two gaps that only showed once somebody tried to use the platform with more
than one person. There was **no way to create an account** except a command
inside the container, which made the row-level authorisation of SESSIONS 23-26
unreachable in practice — there was nobody to assign a scope to. And there was
**no way to change a password at all**: `must_change` existed on the
credential, was reported at login, could never be satisfied, and was enforced
nowhere, so a temporary password worked indefinitely.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.application.auth import AuthError, AuthService
from academic_ai.application.users import (
    UserManagementError,
    create_user,
    list_users,
    set_active,
    set_role,
)
from academic_ai.bootstrap import create_administrator
from academic_ai.domain.enums import UserRole
from academic_ai.infrastructure.db.models import User, UserCredential
from academic_ai.security.passwords import verify_password
from academic_ai.security.rbac import AuthorizationError, Principal

from academic_ai.security.settings import SecuritySettings


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


STRONG = "una-contrasena-suficientemente-larga"
OTHER = "otra-contrasena-igual-de-larga"


def _principal(session: Session, role: UserRole, label: str) -> Principal:
    user = User(
        email=f"{label}-{uuid.uuid4().hex[:6]}@example.test",
        full_name=label,
        primary_role=role,
    )
    session.add(user)
    session.flush()
    return Principal(user_id=str(user.id), role=role)


class TestCreatingAccounts:
    def test_an_account_comes_back_with_a_temporary_password(
        self, session: Session
    ) -> None:
        admin = _principal(session, UserRole.SUPER_ADMIN, "admin")
        account = create_user(
            session,
            admin,
            email="Nuevo@Example.Test",
            full_name="Persona Nueva",
            role=UserRole.COORDINATOR,
        )

        assert account.email == "nuevo@example.test"
        assert account.must_change_password
        assert account.temporary_password
        # Long enough not to be worth guessing.
        assert len(account.temporary_password) >= 20

    def test_the_temporary_password_actually_works(
        self, session: Session, settings
    ) -> None:
        admin = _principal(session, UserRole.SUPER_ADMIN, "admin")
        account = create_user(
            session, admin, email="x@example.test", full_name="X",
            role=UserRole.TEACHER,
        )
        credential = session.scalars(
            select(UserCredential).where(
                UserCredential.app_user_id == uuid.UUID(account.user_id)
            )
        ).one()
        assert verify_password(account.temporary_password, credential.password_hash)

    def test_a_duplicate_email_is_refused(self, session: Session) -> None:
        admin = _principal(session, UserRole.SUPER_ADMIN, "admin")
        create_user(
            session, admin, email="dup@example.test", full_name="A",
            role=UserRole.TEACHER,
        )
        with pytest.raises(UserManagementError, match="already exists"):
            create_user(
                session, admin, email="dup@example.test", full_name="B",
                role=UserRole.TEACHER,
            )

    def test_a_caller_without_manage_users_is_refused(
        self, session: Session
    ) -> None:
        coordinator = _principal(session, UserRole.COORDINATOR, "coord")
        with pytest.raises(AuthorizationError):
            create_user(
                session, coordinator, email="n@example.test", full_name="N",
                role=UserRole.TEACHER,
            )


class TestNobodyReachesAboveTheirOwnRole:
    """Privilege escalation with extra steps, and both directions of it."""

    def test_an_admin_cannot_create_a_super_admin(self, session: Session) -> None:
        admin = _principal(session, UserRole.ADMIN, "admin")
        with pytest.raises(UserManagementError, match="outranks"):
            create_user(
                session, admin, email="sa@example.test", full_name="SA",
                role=UserRole.SUPER_ADMIN,
            )

    def test_an_admin_cannot_promote_to_super_admin(
        self, session: Session
    ) -> None:
        admin = _principal(session, UserRole.ADMIN, "admin")
        target = create_user(
            session, admin, email="t@example.test", full_name="T",
            role=UserRole.COORDINATOR,
        )
        with pytest.raises(UserManagementError, match="outranks"):
            set_role(session, admin, target.user_id, UserRole.SUPER_ADMIN)

    def test_an_admin_cannot_demote_a_super_admin(self, session: Session) -> None:
        # The same escalation seen from the other side: it removes an
        # authority that could have stopped you.
        admin = _principal(session, UserRole.ADMIN, "admin")
        senior = _principal(session, UserRole.SUPER_ADMIN, "senior")
        with pytest.raises(UserManagementError, match="outranks"):
            set_role(session, admin, senior.user_id, UserRole.TEACHER)

    def test_a_super_admin_may_create_anything(self, session: Session) -> None:
        root = _principal(session, UserRole.SUPER_ADMIN, "root")
        account = create_user(
            session, root, email="sa2@example.test", full_name="SA",
            role=UserRole.SUPER_ADMIN,
        )
        assert account.role == "SUPER_ADMIN"

    def test_an_unranked_role_is_treated_as_the_most_junior(self) -> None:
        # Forgetting to place a new role must not grant it seniority.
        from academic_ai.application.users import _SENIORITY, _rank

        assert set(_SENIORITY) == set(UserRole), (
            "a role is missing from the seniority order; place it "
            "deliberately rather than inheriting the enum's order"
        )
        assert _rank(UserRole.STUDENT) > _rank(UserRole.SUPER_ADMIN)


class TestDeactivation:
    def test_an_account_can_be_deactivated_and_restored(
        self, session: Session
    ) -> None:
        admin = _principal(session, UserRole.SUPER_ADMIN, "admin")
        account = create_user(
            session, admin, email="d@example.test", full_name="D",
            role=UserRole.TEACHER,
        )
        assert not set_active(session, admin, account.user_id, False).is_active
        assert set_active(session, admin, account.user_id, True).is_active

    def test_you_cannot_deactivate_yourself(self, session: Session) -> None:
        # The button is next to the one for everybody else, and the way back
        # in is a shell on the container.
        admin = _principal(session, UserRole.SUPER_ADMIN, "admin")
        with pytest.raises(UserManagementError, match="your own account"):
            set_active(session, admin, admin.user_id, False)

    def test_an_admin_cannot_deactivate_a_super_admin(
        self, session: Session
    ) -> None:
        admin = _principal(session, UserRole.ADMIN, "admin")
        senior = _principal(session, UserRole.SUPER_ADMIN, "senior")
        with pytest.raises(UserManagementError, match="outranks"):
            set_active(session, admin, senior.user_id, False)


class TestChangingYourPassword:
    def _service(self, session: Session, settings) -> AuthService:
        return AuthService(session, settings)

    def test_a_password_can_finally_be_changed(
        self, session: Session, settings
    ) -> None:
        user_id = create_administrator(
            session, email="c@example.test", full_name="C", password=STRONG
        )
        auth = self._service(session, settings)
        auth.change_password(user_id, STRONG, OTHER)

        credential = session.scalars(
            select(UserCredential).where(UserCredential.app_user_id == user_id)
        ).one()
        assert verify_password(OTHER, credential.password_hash)
        assert not verify_password(STRONG, credential.password_hash)

    def test_the_current_password_is_required(
        self, session: Session, settings
    ) -> None:
        # Even when a change is being forced. Otherwise the holder of a stolen
        # access token sets a new password and locks the real owner out,
        # turning a token theft into an account theft.
        user_id = create_administrator(
            session, email="c2@example.test", full_name="C", password=STRONG
        )
        auth = self._service(session, settings)
        with pytest.raises(AuthError):
            auth.change_password(user_id, "una-contrasena-equivocada", OTHER)

    def test_reusing_the_same_password_is_refused(
        self, session: Session, settings
    ) -> None:
        user_id = create_administrator(
            session, email="c3@example.test", full_name="C", password=STRONG
        )
        auth = self._service(session, settings)
        with pytest.raises(AuthError, match="differ"):
            auth.change_password(user_id, STRONG, STRONG)

    def test_changing_it_clears_the_forced_flag(
        self, session: Session, settings
    ) -> None:
        admin = _principal(session, UserRole.SUPER_ADMIN, "admin")
        account = create_user(
            session, admin, email="f@example.test", full_name="F",
            role=UserRole.COORDINATOR,
        )
        auth = self._service(session, settings)
        auth.change_password(account.user_id, account.temporary_password, OTHER)

        credential = session.scalars(
            select(UserCredential).where(
                UserCredential.app_user_id == uuid.UUID(account.user_id)
            )
        ).one()
        assert credential.must_change is False

    def test_changing_it_revokes_every_other_session(
        self, session: Session, settings
    ) -> None:
        # Changing a password is what somebody does when they believe it is
        # compromised. Leaving the attacker's refresh token live is the one
        # outcome that makes the act pointless.
        from academic_ai.infrastructure.db.models import UserSession

        user_id = create_administrator(
            session, email="c4@example.test", full_name="C", password=STRONG
        )
        auth = self._service(session, settings)
        auth.login("c4@example.test", STRONG, client_ip="127.0.0.1")
        live = [
            s
            for s in session.scalars(select(UserSession))
            if s.app_user_id == user_id and s.revoked_at is None
        ]
        assert live, "no session to revoke; wrong test"

        auth.change_password(user_id, STRONG, OTHER)
        still_live = [
            s
            for s in session.scalars(select(UserSession))
            if s.app_user_id == user_id and s.revoked_at is None
        ]
        assert still_live == []


class TestListing:
    def test_every_account_is_listed(self, session: Session) -> None:
        admin = _principal(session, UserRole.SUPER_ADMIN, "admin")
        create_user(
            session, admin, email="l1@example.test", full_name="L1",
            role=UserRole.TEACHER,
        )
        emails = {a.email for a in list_users(session, admin)}
        assert "l1@example.test" in emails

    def test_a_temporary_password_is_never_listed(self, session: Session) -> None:
        # It comes back once, on creation, and is never stored in a form that
        # could be read again.
        admin = _principal(session, UserRole.SUPER_ADMIN, "admin")
        create_user(
            session, admin, email="l2@example.test", full_name="L2",
            role=UserRole.TEACHER,
        )
        for account in list_users(session, admin):
            assert account.temporary_password is None
