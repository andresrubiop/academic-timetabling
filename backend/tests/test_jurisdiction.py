"""SESSION 23: where a role's authority reaches.

`rbac.py` answers "may this kind of thing happen". This is the other half:
"may it happen **here**". `Principal.program_ids` and `faculty_ids` existed
from SESSION 8 and were always empty, because nothing in the schema recorded
which programmes a coordinator coordinates — so every scoped role behaved as
though it held the whole institution, and `SECURITY.md` §9 recorded it as the
largest open gap.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from academic_ai.domain.enums import UserRole
from academic_ai.infrastructure.db.models import Program, User, UserScopeAssignment
from academic_ai.infrastructure.repositories import (
    load_jurisdiction,
    load_jurisdictions,
)
from academic_ai.security.jurisdiction import (
    Breadth,
    Jurisdiction,
    breadth_for,
    jurisdiction_for,
)
from academic_ai.security.rbac import Principal


class TestBreadth:
    def test_each_role_has_the_breadth_its_post_implies(self) -> None:
        assert breadth_for(UserRole.SUPER_ADMIN) is Breadth.INSTITUTION
        assert breadth_for(UserRole.ADMIN) is Breadth.INSTITUTION
        assert breadth_for(UserRole.DEAN) is Breadth.FACULTY
        assert breadth_for(UserRole.DIRECTOR) is Breadth.PROGRAM
        assert breadth_for(UserRole.COORDINATOR) is Breadth.PROGRAM
        assert breadth_for(UserRole.TEACHER) is Breadth.OWN
        assert breadth_for(UserRole.STUDENT) is Breadth.OWN

    def test_every_role_is_covered(self) -> None:
        # A role missing from the table falls to OWN, but silently defaulting
        # is not the same as having decided.
        from academic_ai.security.jurisdiction import ROLE_BREADTH

        assert set(ROLE_BREADTH) == set(UserRole)


class TestCoverage:
    def test_an_institution_wide_role_covers_everything(self) -> None:
        # Including programmes that do not exist yet: an admin must be able to
        # administer the platform before anybody has been assigned anything,
        # or the first deployment has nobody who can create the assignments.
        admin = jurisdiction_for(UserRole.ADMIN)
        assert admin.covers_program(str(uuid.uuid4()))
        assert admin.covers_faculty(str(uuid.uuid4()))
        assert admin.covers_any_program([])

    def test_a_scoped_role_covers_only_what_it_holds(self) -> None:
        coordinator = jurisdiction_for(UserRole.COORDINATOR, program_ids=["p1"])
        assert coordinator.covers_program("p1")
        assert not coordinator.covers_program("p2")

    def test_an_unassigned_scoped_role_covers_nothing(self) -> None:
        # The rule the whole module exists for. The opposite default would let
        # every existing account keep working after this shipped, and would
        # mean the check never refused anything -- indistinguishable from not
        # having written it.
        coordinator = jurisdiction_for(UserRole.COORDINATOR)
        assert coordinator.is_empty
        assert not coordinator.covers_program("p1")
        assert not coordinator.covers_any_program(["p1", "p2"])

    def test_an_institution_wide_role_is_never_empty(self) -> None:
        assert not jurisdiction_for(UserRole.ADMIN).is_empty

    def test_covering_any_means_any_not_all(self) -> None:
        # A change touching four programmes is the business of every
        # coordinator holding one of them. Requiring all four would mean only
        # an institution-wide role could ever be involved in a cross-programme
        # change -- precisely the change that most needs a programme's own
        # coordinator to look at it.
        coordinator = jurisdiction_for(UserRole.COORDINATOR, program_ids=["p2"])
        assert coordinator.covers_any_program(["p1", "p2", "p3"])
        assert not coordinator.covers_any_program(["p1", "p3"])

    def test_nothing_covers_a_missing_id(self) -> None:
        coordinator = jurisdiction_for(UserRole.COORDINATOR, program_ids=["p1"])
        assert not coordinator.covers_program(None)
        assert not coordinator.covers_faculty(None)


class TestPrincipal:
    def test_the_jurisdiction_follows_the_role(self) -> None:
        # Derived rather than stored, so a principal built with a
        # coordinator's role and an admin's breadth is not constructible.
        principal = Principal(
            user_id="u", role=UserRole.COORDINATOR, program_ids=frozenset({"p1"})
        )
        assert principal.jurisdiction.breadth is Breadth.PROGRAM
        assert principal.jurisdiction.covers_program("p1")

    def test_an_admin_principal_covers_everything_without_assignments(self) -> None:
        principal = Principal(user_id="u", role=UserRole.ADMIN)
        assert principal.jurisdiction.covers_program("anything at all")


class TestLoadingFromTheDatabase:
    def _user(self, session: Session, world, role: UserRole) -> User:
        user = User(
            email=f"{role.value.lower()}-{uuid.uuid4().hex[:6]}@example.test",
            full_name=f"Prueba {role.value}",
            primary_role=role,
        )
        session.add(user)
        session.flush()
        return user

    def test_an_assigned_programme_is_read_back(
        self, session: Session, world
    ) -> None:
        user = self._user(session, world, UserRole.COORDINATOR)
        session.add(
            UserScopeAssignment(app_user_id=user.id, program_id=world.program.id)
        )
        session.flush()

        j = load_jurisdiction(session, user.id, UserRole.COORDINATOR)
        assert j.covers_program(str(world.program.id))
        assert not j.covers_program(str(world.other_program.id))

    def test_a_faculty_assignment_expands_to_its_programmes(
        self, session: Session, world
    ) -> None:
        # Expansion happens once, in the read. A check that walked the
        # hierarchy would put a query inside an authorisation decision, and
        # those get cached, and cached authorisation decisions go stale
        # exactly when somebody is removed from a post.
        dean = self._user(session, world, UserRole.DEAN)
        session.add(
            UserScopeAssignment(app_user_id=dean.id, faculty_id=world.faculty.id)
        )
        session.flush()

        j = load_jurisdiction(session, dean.id, UserRole.DEAN)
        assert j.covers_faculty(str(world.faculty.id))
        # Both seeded programmes sit under the seeded faculty.
        assert j.covers_program(str(world.program.id))
        assert j.covers_program(str(world.other_program.id))

    def test_an_unassigned_coordinator_holds_nothing(
        self, session: Session, world
    ) -> None:
        user = self._user(session, world, UserRole.COORDINATOR)
        j = load_jurisdiction(session, user.id, UserRole.COORDINATOR)
        assert j.is_empty

    def test_an_institution_role_is_not_queried(
        self, session: Session, world
    ) -> None:
        # Short-circuited: it is not made wider or narrower by an assignment,
        # and reading the table for it would be a query on every request by
        # the accounts that make the most of them.
        admin = self._user(session, world, UserRole.ADMIN)
        j = load_jurisdiction(session, admin.id, UserRole.ADMIN)
        assert j.is_institution_wide
        assert j.covers_program(str(uuid.uuid4()))

    def test_several_users_are_loaded_in_one_go(
        self, session: Session, world
    ) -> None:
        a = self._user(session, world, UserRole.COORDINATOR)
        b = self._user(session, world, UserRole.COORDINATOR)
        session.add(UserScopeAssignment(app_user_id=a.id, program_id=world.program.id))
        session.flush()

        loaded = load_jurisdictions(
            session, [a.id, b.id], UserRole.COORDINATOR
        )
        assert loaded[str(a.id)].covers_program(str(world.program.id))
        assert loaded[str(b.id)].is_empty

    def test_no_users_is_not_a_query(self, session: Session) -> None:
        assert load_jurisdictions(session, [], UserRole.COORDINATOR) == {}


class TestTheSchemaRefusesNonsense:
    def test_an_assignment_needs_exactly_one_target(
        self, session: Session, world
    ) -> None:
        from sqlalchemy.exc import IntegrityError

        user = User(
            email=f"x-{uuid.uuid4().hex[:6]}@example.test",
            full_name="X",
            primary_role=UserRole.COORDINATOR,
        )
        session.add(user)
        session.flush()

        # Neither.
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(UserScopeAssignment(app_user_id=user.id))
                session.flush()

        # Both — a row that would mean two different things at once.
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    UserScopeAssignment(
                        app_user_id=user.id,
                        faculty_id=world.faculty.id,
                        program_id=world.program.id,
                    )
                )
                session.flush()

    def test_the_same_programme_cannot_be_assigned_twice(
        self, session: Session, world
    ) -> None:
        # It would double that programme's weight in any count of who is
        # responsible for it.
        from sqlalchemy.exc import IntegrityError

        user = User(
            email=f"y-{uuid.uuid4().hex[:6]}@example.test",
            full_name="Y",
            primary_role=UserRole.COORDINATOR,
        )
        session.add(user)
        session.flush()
        session.add(
            UserScopeAssignment(app_user_id=user.id, program_id=world.program.id)
        )
        session.flush()

        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    UserScopeAssignment(
                        app_user_id=user.id, program_id=world.program.id
                    )
                )
                session.flush()

    def test_an_assignment_to_a_programme_that_does_not_exist_is_refused(
        self, session: Session, world
    ) -> None:
        # The reason this is a row per assignment with a foreign key rather
        # than an array of uuids: an array refuses nothing.
        from sqlalchemy.exc import IntegrityError

        user = User(
            email=f"z-{uuid.uuid4().hex[:6]}@example.test",
            full_name="Z",
            primary_role=UserRole.COORDINATOR,
        )
        session.add(user)
        session.flush()

        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    UserScopeAssignment(app_user_id=user.id, program_id=uuid.uuid4())
                )
                session.flush()
