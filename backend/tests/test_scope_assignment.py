"""SESSION 24: granting and revoking authority.

SESSION 23 built `user_scope_assignment` and the model that reads it, and
nothing but tests ever wrote to it — so every scoped role held nothing and
enforcement could not be switched on. This is the write side.

Two rules make it more than a CRUD table: nobody may grant authority they do
not hold, and every grant and revoke is audited.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.domain.enums import AuditAction, UserRole
from academic_ai.infrastructure.db.models import AuditLog, User, UserScopeAssignment
from academic_ai.application.scopes import (
    ScopeAssignmentError,
    grant_scope,
    list_scopes,
    revoke_scope,
)
from academic_ai.security.rbac import AuthorizationError, Principal


def _user(session: Session, role: UserRole, label: str) -> User:
    user = User(
        email=f"{label}-{uuid.uuid4().hex[:6]}@example.test",
        full_name=f"Prueba {label}",
        primary_role=role,
    )
    session.add(user)
    session.flush()
    return user


def _admin(session: Session) -> Principal:
    user = _user(session, UserRole.ADMIN, "admin")
    return Principal(user_id=str(user.id), role=UserRole.ADMIN)


class TestGranting:
    def test_a_programme_can_be_assigned(self, session: Session, world) -> None:
        admin = _admin(session)
        coordinator = _user(session, UserRole.COORDINATOR, "coord")

        grant = grant_scope(
            session,
            admin,
            coordinator.id,
            program_id=world.program.id,
            note="Coordinadora desde 2026-03",
        )

        assert grant.program_id == str(world.program.id)
        assert grant.faculty_id is None
        assert grant.note

    def test_the_grant_is_visible_to_the_jurisdiction_read(
        self, session: Session, world
    ) -> None:
        # The whole point: the assignment has to reach `Principal`, or it is a
        # row in a table nothing consults.
        from academic_ai.infrastructure.repositories import load_jurisdiction

        admin = _admin(session)
        coordinator = _user(session, UserRole.COORDINATOR, "coord")
        grant_scope(session, admin, coordinator.id, program_id=world.program.id)

        j = load_jurisdiction(session, coordinator.id, UserRole.COORDINATOR)
        assert j.covers_program(str(world.program.id))

    def test_naming_both_targets_is_refused(self, session: Session, world) -> None:
        admin = _admin(session)
        coordinator = _user(session, UserRole.COORDINATOR, "coord")
        with pytest.raises(ScopeAssignmentError, match="exactly one"):
            grant_scope(
                session,
                admin,
                coordinator.id,
                faculty_id=world.faculty.id,
                program_id=world.program.id,
            )

    def test_naming_neither_is_refused(self, session: Session) -> None:
        admin = _admin(session)
        coordinator = _user(session, UserRole.COORDINATOR, "coord")
        with pytest.raises(ScopeAssignmentError, match="exactly one"):
            grant_scope(session, admin, coordinator.id)

    def test_assigning_a_teacher_is_refused(self, session: Session, world) -> None:
        # A teacher holds authority over nobody, so the row would be inert —
        # and an inert row here is worse than none, because an audit of "who
        # is responsible for this programme" would list them.
        admin = _admin(session)
        teacher = _user(session, UserRole.TEACHER, "teacher")
        with pytest.raises(ScopeAssignmentError, match="no authority"):
            grant_scope(session, admin, teacher.id, program_id=world.program.id)

    def test_an_unknown_user_is_refused(self, session: Session, world) -> None:
        admin = _admin(session)
        with pytest.raises(ScopeAssignmentError, match="no user"):
            grant_scope(session, admin, uuid.uuid4(), program_id=world.program.id)

    def test_a_caller_without_manage_users_is_refused(
        self, session: Session, world
    ) -> None:
        coordinator_principal = Principal(
            user_id=str(uuid.uuid4()), role=UserRole.COORDINATOR
        )
        target = _user(session, UserRole.COORDINATOR, "coord")
        with pytest.raises(AuthorizationError):
            grant_scope(
                session, coordinator_principal, target.id, program_id=world.program.id
            )


class TestNobodyGrantsWhatTheyDoNotHold:
    """Refuses nothing today — `MANAGE_USERS` belongs only to institution-wide
    roles — which is the argument for writing it now rather than later.
    Delegation is how a scoped administrator escalates."""

    def test_an_institution_wide_admin_may_assign_anything(
        self, session: Session, world
    ) -> None:
        admin = _admin(session)
        coordinator = _user(session, UserRole.COORDINATOR, "coord")
        grant_scope(session, admin, coordinator.id, faculty_id=world.faculty.id)

    def test_a_scoped_grantor_cannot_reach_outside_their_faculty(
        self, session: Session, world
    ) -> None:
        # Constructed directly: no role holds both MANAGE_USERS and a narrow
        # breadth today. The check exists for the day one does.
        scoped_admin = Principal(
            user_id=str(_user(session, UserRole.ADMIN, "dean-ish").id),
            role=UserRole.DEAN,
            faculty_ids=frozenset({str(uuid.uuid4())}),
        )
        # A DEAN has no MANAGE_USERS, so authorise() refuses first. Assert the
        # scope rule directly instead, which is the thing under test.
        from academic_ai.application.scopes import (
            _refuse_granting_beyond_own_authority,
        )

        with pytest.raises(ScopeAssignmentError, match="faculty you do not hold"):
            _refuse_granting_beyond_own_authority(
                scoped_admin, faculty_id=str(world.faculty.id), program_id=None
            )

    def test_a_scoped_grantor_may_assign_inside_their_own(
        self, session: Session, world
    ) -> None:
        from academic_ai.application.scopes import (
            _refuse_granting_beyond_own_authority,
        )

        scoped = Principal(
            user_id=str(uuid.uuid4()),
            role=UserRole.DEAN,
            faculty_ids=frozenset({str(world.faculty.id)}),
        )
        _refuse_granting_beyond_own_authority(
            scoped, faculty_id=str(world.faculty.id), program_id=None
        )  # does not raise


class TestRevoking:
    def test_an_assignment_can_be_taken_away(
        self, session: Session, world
    ) -> None:
        from academic_ai.infrastructure.repositories import load_jurisdiction

        admin = _admin(session)
        coordinator = _user(session, UserRole.COORDINATOR, "coord")
        grant = grant_scope(
            session, admin, coordinator.id, program_id=world.program.id
        )

        revoke_scope(session, admin, grant.assignment_id)

        assert load_jurisdiction(
            session, coordinator.id, UserRole.COORDINATOR
        ).is_empty

    def test_revoking_something_that_is_not_there_is_refused(
        self, session: Session
    ) -> None:
        admin = _admin(session)
        with pytest.raises(ScopeAssignmentError, match="no scope assignment"):
            revoke_scope(session, admin, uuid.uuid4())


class TestAudit:
    """`audit_log` has existed since SESSION 1, append-only by trigger, and
    nothing ever wrote to it. Handing somebody authority over a faculty is the
    canonical thing an audit trail is for."""

    def _entries(self, session: Session) -> list[AuditLog]:
        return list(
            session.scalars(
                select(AuditLog).where(
                    AuditLog.entity_type == "user_scope_assignment"
                )
            )
        )

    def test_a_grant_is_recorded(self, session: Session, world) -> None:
        admin = _admin(session)
        coordinator = _user(session, UserRole.COORDINATOR, "coord")
        grant_scope(session, admin, coordinator.id, program_id=world.program.id)

        entries = self._entries(session)
        assert len(entries) == 1
        assert entries[0].action is AuditAction.CREATE
        assert entries[0].actor_user_id is not None

    def test_a_revoke_keeps_what_the_row_said(
        self, session: Session, world
    ) -> None:
        # The row is gone after a revoke, so "who used to be responsible" only
        # survives if the audit entry carries it.
        admin = _admin(session)
        coordinator = _user(session, UserRole.COORDINATOR, "coord")
        grant = grant_scope(
            session, admin, coordinator.id, program_id=world.program.id
        )
        revoke_scope(session, admin, grant.assignment_id)

        revoked = [e for e in self._entries(session) if e.action is AuditAction.REVOKE]
        assert len(revoked) == 1
        assert revoked[0].before["program_id"] == str(world.program.id)
        assert revoked[0].before["app_user_id"] == str(coordinator.id)

    def test_the_actor_is_named(self, session: Session, world) -> None:
        # An assignment with no name against it is an anonymous grant of
        # authority.
        admin = _admin(session)
        coordinator = _user(session, UserRole.COORDINATOR, "coord")
        grant_scope(session, admin, coordinator.id, program_id=world.program.id)

        entry = self._entries(session)[0]
        assert str(entry.actor_user_id) == admin.user_id
        assert entry.actor_description


class TestListing:
    def test_every_scope_a_user_holds_is_listed(
        self, session: Session, world
    ) -> None:
        admin = _admin(session)
        director = _user(session, UserRole.DIRECTOR, "dir")
        grant_scope(session, admin, director.id, program_id=world.program.id)
        grant_scope(session, admin, director.id, program_id=world.other_program.id)

        assert len(list_scopes(session, admin, director.id)) == 2

    def test_a_user_with_nothing_lists_nothing(self, session: Session) -> None:
        admin = _admin(session)
        nobody = _user(session, UserRole.COORDINATOR, "nobody")
        assert list_scopes(session, admin, nobody.id) == []
