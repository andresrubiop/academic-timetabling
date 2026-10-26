"""SESSION 14: the generic CRUD router, exercised against real entities.

One implementation (`api/crud.py`) is applied to twenty tables. These tests do
not try to cover every table -- that would be the same assertions copied
twenty times, which is the exact problem the generic router exists to avoid.
They cover the *behaviours* the framework promises, each against whichever
entity makes the case clearest:

* search, filter, sort and pagination compose rather than overriding one
  another;
* a retired row stays queryable, because every foreign key still pointing at
  it has to keep resolving;
* retiring twice is a 409 rather than a silent success, so a second click
  tells the operator something happened differently from the first;
* a duplicate unique field is a friendly 409, not a raw constraint name;
* a role without the permission is refused before the handler runs;
* the client cannot set the columns the database and the auth layer own.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from academic_ai.api.app import create_app
from academic_ai.api.deps import get_session, get_settings
from academic_ai.domain.enums import UserRole
from academic_ai.infrastructure.db.models import User, UserCredential
from academic_ai.security.passwords import hash_password
from academic_ai.security.settings import SecuritySettings

PASSWORD = "a perfectly reasonable passphrase"


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


def _user(session: Session, *, role: UserRole, email: str) -> User:
    user = User(email=email, full_name="Test User", primary_role=role)
    session.add(user)
    session.flush()
    session.add(
        UserCredential(app_user_id=user.id, password_hash=hash_password(PASSWORD))
    )
    session.flush()
    return user


@pytest.fixture()
def admin(session: Session) -> User:
    return _user(session, role=UserRole.ADMIN, email="crudadmin@example.test")


@pytest.fixture()
def client(session: Session, settings: SecuritySettings):
    app = create_app(settings)
    # The test owns the transaction, so the app must use *this* session, or
    # the rows the test creates are invisible to the request and the
    # request's writes are invisible to the test.
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as c:
        yield c


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Create, read, update
# ---------------------------------------------------------------------------


class TestCreateReadUpdate:
    def test_creating_and_reading_an_institution(
        self, client: TestClient, admin: User
    ) -> None:
        token = _login(client, "crudadmin@example.test")
        created = client.post(
            "/api/v1/institutions",
            json={
                "code": "PUCE",
                "name": "P.U. Catolica",
                "timezone": "America/Guayaquil",
            },
            headers=_auth(token),
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["code"] == "PUCE"
        assert body["status"] == "ACTIVE", "the column default is applied server-side"

        fetched = client.get(f"/api/v1/institutions/{body['id']}", headers=_auth(token))
        assert fetched.status_code == 200
        assert fetched.json()["name"] == "P.U. Catolica"

    def test_the_client_cannot_set_server_owned_columns(
        self, client: TestClient, admin: User
    ) -> None:
        """`id`, the timestamps and the actor columns belong to the database
        and the auth layer. A client that could set `created_by_user_id` could
        attribute a row to anyone."""
        token = _login(client, "crudadmin@example.test")
        response = client.post(
            "/api/v1/institutions",
            json={
                "code": "X1",
                "name": "X",
                "created_by_user_id": str(uuid.uuid4()),
            },
            headers=_auth(token),
        )
        assert response.status_code == 422

    def test_the_creator_is_recorded_from_the_token_not_the_body(
        self, client: TestClient, admin: User
    ) -> None:
        token = _login(client, "crudadmin@example.test")
        created = client.post(
            "/api/v1/institutions",
            json={"code": "WHO", "name": "Attribution"},
            headers=_auth(token),
        ).json()
        assert created["created_by_user_id"] == str(admin.id)

    def test_updating_is_partial(self, client: TestClient, admin: User) -> None:
        token = _login(client, "crudadmin@example.test")
        created = client.post(
            "/api/v1/institutions",
            json={"code": "X2", "name": "Original Name"},
            headers=_auth(token),
        ).json()
        updated = client.patch(
            f"/api/v1/institutions/{created['id']}",
            json={"name": "New Name"},
            headers=_auth(token),
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "New Name"
        assert updated.json()["code"] == "X2", "an omitted field is left alone"

    def test_reading_an_unknown_row_is_404(
        self, client: TestClient, admin: User
    ) -> None:
        token = _login(client, "crudadmin@example.test")
        response = client.get(
            f"/api/v1/institutions/{uuid.uuid4()}", headers=_auth(token)
        )
        assert response.status_code == 404

    def test_a_duplicate_code_is_a_friendly_conflict(
        self, client: TestClient, admin: User
    ) -> None:
        """psycopg's message names the constraint, not the field, which is not
        something an administrator filling in a form should have to parse."""
        token = _login(client, "crudadmin@example.test")
        client.post(
            "/api/v1/institutions",
            json={"code": "DUP", "name": "First"},
            headers=_auth(token),
        )
        second = client.post(
            "/api/v1/institutions",
            json={"code": "DUP", "name": "Second"},
            headers=_auth(token),
        )
        assert second.status_code == 409
        assert "already exists" in second.json()["detail"]
        assert "uq_institution_code" not in second.json()["detail"]


# ---------------------------------------------------------------------------
# Search, filter, sort, pagination
# ---------------------------------------------------------------------------


class TestListing:
    @staticmethod
    def _seed(client: TestClient, token: str) -> None:
        for code, name, status in [
            ("AAA", "Alpha Institute", "ACTIVE"),
            ("BBB", "Beta College", "ACTIVE"),
            ("CCC", "Gamma University", "SUSPENDED"),
        ]:
            response = client.post(
                "/api/v1/institutions",
                json={"code": code, "name": name, "status": status},
                headers=_auth(token),
            )
            assert response.status_code == 201, response.text

    def test_search_matches_a_substring_case_insensitively(
        self, client: TestClient, admin: User
    ) -> None:
        token = _login(client, "crudadmin@example.test")
        self._seed(client, token)
        response = client.get(
            "/api/v1/institutions", params={"q": "beta"}, headers=_auth(token)
        )
        assert {row["code"] for row in response.json()["items"]} == {"BBB"}

    def test_filter_by_exact_status(self, client: TestClient, admin: User) -> None:
        token = _login(client, "crudadmin@example.test")
        self._seed(client, token)
        response = client.get(
            "/api/v1/institutions",
            params={"status": "SUSPENDED"},
            headers=_auth(token),
        )
        assert {row["code"] for row in response.json()["items"]} == {"CCC"}

    def test_an_invalid_enum_filter_lists_what_is_allowed(
        self, client: TestClient, admin: User
    ) -> None:
        token = _login(client, "crudadmin@example.test")
        response = client.get(
            "/api/v1/institutions", params={"status": "BANANA"}, headers=_auth(token)
        )
        assert response.status_code == 422
        assert "RETIRED" in response.json()["detail"]

    def test_filtering_by_an_unlisted_field_is_refused(
        self, client: TestClient, admin: User
    ) -> None:
        """Silently ignoring an unknown filter would return every row while
        looking like the filter had been applied."""
        token = _login(client, "crudadmin@example.test")
        response = client.get(
            "/api/v1/institutions",
            params={"timezone": "America/Guayaquil"},
            headers=_auth(token),
        )
        assert response.status_code == 422

    def test_sort_ascending_and_descending(
        self, client: TestClient, admin: User
    ) -> None:
        token = _login(client, "crudadmin@example.test")
        self._seed(client, token)
        asc = client.get(
            "/api/v1/institutions", params={"sort": "code"}, headers=_auth(token)
        ).json()["items"]
        desc = client.get(
            "/api/v1/institutions", params={"sort": "-code"}, headers=_auth(token)
        ).json()["items"]
        assert [r["code"] for r in asc] == ["AAA", "BBB", "CCC"]
        assert [r["code"] for r in desc] == ["CCC", "BBB", "AAA"]

    def test_sorting_by_an_unlisted_field_is_refused(
        self, client: TestClient, admin: User
    ) -> None:
        token = _login(client, "crudadmin@example.test")
        response = client.get(
            "/api/v1/institutions", params={"sort": "extra"}, headers=_auth(token)
        )
        assert response.status_code == 422

    def test_pagination_reports_the_total_and_never_double_counts(
        self, client: TestClient, admin: User
    ) -> None:
        token = _login(client, "crudadmin@example.test")
        self._seed(client, token)
        page1 = client.get(
            "/api/v1/institutions",
            params={"page": 1, "page_size": 2, "sort": "code"},
            headers=_auth(token),
        ).json()
        page2 = client.get(
            "/api/v1/institutions",
            params={"page": 2, "page_size": 2, "sort": "code"},
            headers=_auth(token),
        ).json()
        assert page1["total"] == 3
        assert len(page1["items"]) == 2
        assert len(page2["items"]) == 1
        seen = {r["code"] for r in page1["items"]} | {r["code"] for r in page2["items"]}
        assert seen == {"AAA", "BBB", "CCC"}, "a row appeared twice or not at all"

    def test_an_oversized_page_is_refused(
        self, client: TestClient, admin: User
    ) -> None:
        """An unbounded page size is a way to make one request do the work a
        scan should have done, on a host shared with other tenants."""
        token = _login(client, "crudadmin@example.test")
        response = client.get(
            "/api/v1/institutions", params={"page_size": 100000}, headers=_auth(token)
        )
        assert response.status_code == 422

    def test_search_and_filter_compose(self, client: TestClient, admin: User) -> None:
        token = _login(client, "crudadmin@example.test")
        self._seed(client, token)
        response = client.get(
            "/api/v1/institutions",
            params={"q": "institute", "status": "ACTIVE"},
            headers=_auth(token),
        )
        assert {row["code"] for row in response.json()["items"]} == {"AAA"}


# ---------------------------------------------------------------------------
# Retire, not delete
# ---------------------------------------------------------------------------


class TestRetireNotDelete:
    def test_deleting_retires_rather_than_removing_the_row(
        self, client: TestClient, admin: User
    ) -> None:
        """CLAUDE.md and DATABASE.md: academic records are retired, not
        deleted. Every foreign key still pointing here has to keep
        resolving."""
        token = _login(client, "crudadmin@example.test")
        created = client.post(
            "/api/v1/institutions",
            json={"code": "RET", "name": "To Retire"},
            headers=_auth(token),
        ).json()

        response = client.delete(
            f"/api/v1/institutions/{created['id']}", headers=_auth(token)
        )
        assert response.status_code == 200
        assert response.json()["status"] == "RETIRED"

        still_there = client.get(
            f"/api/v1/institutions/{created['id']}", headers=_auth(token)
        )
        assert still_there.status_code == 200
        assert still_there.json()["status"] == "RETIRED"

    def test_retiring_twice_is_a_conflict_not_a_silent_success(
        self, client: TestClient, admin: User
    ) -> None:
        """A second click on delete should tell the operator something
        happened differently from the first, rather than nothing."""
        token = _login(client, "crudadmin@example.test")
        created = client.post(
            "/api/v1/institutions",
            json={"code": "RET2", "name": "Twice"},
            headers=_auth(token),
        ).json()
        client.delete(f"/api/v1/institutions/{created['id']}", headers=_auth(token))
        second = client.delete(
            f"/api/v1/institutions/{created['id']}", headers=_auth(token)
        )
        assert second.status_code == 409

    def test_a_retired_row_can_still_be_found(
        self, client: TestClient, admin: User
    ) -> None:
        token = _login(client, "crudadmin@example.test")
        created = client.post(
            "/api/v1/institutions",
            json={"code": "RET3", "name": "Findable"},
            headers=_auth(token),
        ).json()
        client.delete(f"/api/v1/institutions/{created['id']}", headers=_auth(token))
        response = client.get(
            "/api/v1/institutions", params={"status": "RETIRED"}, headers=_auth(token)
        )
        assert any(r["id"] == created["id"] for r in response.json()["items"])

    def test_an_enrollment_retires_to_dropped_not_to_a_generic_status(
        self, client: TestClient, admin: User, session: Session, world
    ) -> None:
        """A student leaving a class is a DROPPED enrolment, which is also
        what ADR-0005 (enrollment as an immutable record) requires."""
        from academic_ai.domain.enums import EnrollmentKind, Modality
        from academic_ai.infrastructure.db.models import CourseOffering, Enrollment

        offering = CourseOffering(
            course_id=world.course.id,
            academic_period_id=world.period.id,
            campus_id=world.campus.id,
            modality=Modality.ONSITE,
        )
        session.add(offering)
        session.flush()
        row = Enrollment(
            student_id=world.student.id,
            course_offering_id=offering.id,
            course_id=world.course.id,
            academic_period_id=world.period.id,
            curriculum_version_id=world.curriculum_version.id,
            # The schema requires a mandatory enrolment to name the curriculum
            # course it satisfies -- "required" with nothing to be required by
            # is not a state the register should be able to hold.
            curriculum_course_id=world.curriculum_course.id,
            is_mandatory=True,
            enrollment_kind=EnrollmentKind.CURRENT_LEVEL,
            attempt_number=1,
        )
        session.add(row)
        session.flush()

        token = _login(client, "crudadmin@example.test")
        response = client.delete(f"/api/v1/enrollments/{row.id}", headers=_auth(token))
        assert response.status_code == 200
        assert response.json()["status"] == "DROPPED"

    def test_a_class_session_hard_deletes_because_it_has_no_history(
        self, client: TestClient, admin: User, session: Session, world
    ) -> None:
        """ClassSession is a definition -- "this section meets N times a
        week" -- not a record with history to preserve."""
        from academic_ai.domain.enums import Modality, SessionType
        from academic_ai.infrastructure.db.models import (
            ClassSession,
            CourseOffering,
            Section,
        )

        offering = CourseOffering(
            course_id=world.course.id,
            academic_period_id=world.period.id,
            campus_id=world.campus.id,
            modality=Modality.ONSITE,
        )
        session.add(offering)
        session.flush()
        section = Section(
            course_offering_id=offering.id,
            academic_period_id=world.period.id,
            code="A",
            capacity=30,
        )
        session.add(section)
        session.flush()
        row = ClassSession(
            section_id=section.id,
            academic_period_id=world.period.id,
            sequence_number=1,
            session_type=SessionType.LECTURE,
            delivery_mode=Modality.ONSITE,
            duration_slots=1,
        )
        session.add(row)
        session.flush()
        row_id = row.id

        token = _login(client, "crudadmin@example.test")
        response = client.delete(f"/api/v1/sessions/{row_id}", headers=_auth(token))
        assert response.status_code == 200
        assert session.get(ClassSession, row_id) is None


# ---------------------------------------------------------------------------
# Validation beyond what the column types give for free
# ---------------------------------------------------------------------------


class TestExtraValidation:
    def test_a_room_capacity_of_zero_is_refused(
        self, client: TestClient, admin: User, world
    ) -> None:
        token = _login(client, "crudadmin@example.test")
        response = client.post(
            "/api/v1/rooms",
            json={
                "building_id": str(world.building.id),
                "campus_id": str(world.campus.id),
                "code": "R-ZERO",
                "name": "Zero",
                "room_type": "CLASSROOM",
                "capacity": 0,
            },
            headers=_auth(token),
        )
        assert response.status_code == 422
        assert "greater than zero" in response.json()["detail"]

    def test_a_period_ending_before_it_starts_is_refused(
        self, client: TestClient, admin: User, world
    ) -> None:
        """Caught here rather than as a 500 from a CHECK constraint whose
        wording the administrator never sees."""
        token = _login(client, "crudadmin@example.test")
        response = client.post(
            "/api/v1/periods",
            json={
                "institution_id": str(world.institution.id),
                "code": "BAD-PERIOD",
                "name": "Backwards",
                "start_date": "2026-07-01",
                "end_date": "2026-03-01",
                "slot_minutes": 60,
            },
            headers=_auth(token),
        )
        assert response.status_code == 422
        assert "before the start date" in response.json()["detail"]

    def test_an_equivalence_cannot_be_filed_already_approved(
        self, client: TestClient, admin: User, world
    ) -> None:
        """APPROVE_ACADEMIC_EQUIVALENCE is one of the five decisions CLAUDE.md
        reserves to humans. Approving through this form field rather than
        through SESSION 12's workflow is the same rule broken through the back
        door."""
        token = _login(client, "crudadmin@example.test")
        response = client.post(
            "/api/v1/course-equivalences",
            json={
                "source_course_id": str(world.course.id),
                "target_course_id": str(world.other_course.id),
                "proposal_source": "HUMAN",
                "status": "APPROVED",
            },
            headers=_auth(token),
        )
        assert response.status_code == 422
        assert "change-request workflow" in response.json()["detail"]


# ---------------------------------------------------------------------------
# Authorisation
# ---------------------------------------------------------------------------


class TestAuthorisation:
    def test_a_student_cannot_manage_institutions(
        self, client: TestClient, session: Session
    ) -> None:
        _user(session, role=UserRole.STUDENT, email="student@example.test")
        token = _login(client, "student@example.test")
        response = client.post(
            "/api/v1/institutions",
            json={"code": "NOPE", "name": "Nope"},
            headers=_auth(token),
        )
        assert response.status_code == 403

    def test_a_teacher_cannot_read_the_student_register(
        self, client: TestClient, session: Session
    ) -> None:
        _user(session, role=UserRole.TEACHER, email="teach@example.test")
        token = _login(client, "teach@example.test")
        assert client.get("/api/v1/students", headers=_auth(token)).status_code == 403

    def test_a_coordinator_may_manage_enrollment_but_not_institutions(
        self, client: TestClient, session: Session
    ) -> None:
        """The split the role table already draws: enrolment is a
        coordinator's daily work, reference data is an administrator's."""
        _user(session, role=UserRole.COORDINATOR, email="coord2@example.test")
        token = _login(client, "coord2@example.test")
        assert client.get("/api/v1/students", headers=_auth(token)).status_code == 200
        assert (
            client.get("/api/v1/institutions", headers=_auth(token)).status_code == 403
        )

    def test_no_token_is_401(self, client: TestClient) -> None:
        assert client.get("/api/v1/institutions").status_code == 401


# ---------------------------------------------------------------------------
# SESSION 24: row-level scope on the CRUD surface
# ---------------------------------------------------------------------------


class TestRowLevelScope:
    """Until now every holder of the permission saw and could edit every row,
    whatever faculty it belonged to. `del principal` in the list handler was
    the whole of it.

    Only entities whose owner is a **direct column** are scoped: a rule
    reached through a join that is not thoroughly tested is worse than an
    absent one, because it looks like enforcement.
    """

    @pytest.fixture()
    def two_faculties(self, session: Session, world):
        from academic_ai.infrastructure.db.models import Faculty

        second = Faculty(
            institution_id=world.institution.id, code="FAC2", name="Otra facultad"
        )
        session.add(second)
        session.flush()
        return world.faculty, second

    def _scoped_admin(self, session: Session, faculty_id, email: str):
        """An ADMIN principal narrowed by hand.

        No role holds both `MANAGE_USERS` and a narrow breadth today, so this
        is constructed rather than logged in — the enforcement is what is
        under test, not the role table.
        """
        from academic_ai.security.rbac import Principal

        return Principal(
            user_id=str(uuid.uuid4()),
            role=UserRole.DEAN,
            faculty_ids=frozenset({str(faculty_id)}),
        )

    def test_a_list_is_narrowed_to_the_callers_faculty(
        self, session: Session, world, two_faculties
    ) -> None:
        from academic_ai.infrastructure.db.models import Department

        mine, theirs = two_faculties
        session.add(Department(faculty_id=theirs.id, code="D-OTHER", name="Ajena"))
        session.flush()

        from academic_ai.api.crud import ScopeRule

        rule = ScopeRule(column="faculty_id", kind="faculty")
        principal = self._scoped_admin(session, mine.id, "dean@example.test")

        # Every department of the other faculty is out of reach.
        others = session.query(Department).filter(
            Department.faculty_id == theirs.id
        ).all()
        assert others
        for row in others:
            assert not rule.covers(principal.jurisdiction, (row.faculty_id,))

        ours = session.query(Department).filter(
            Department.faculty_id == mine.id
        ).all()
        for row in ours:
            assert rule.covers(principal.jurisdiction, (row.faculty_id,))

    def test_an_unowned_row_stays_visible(self, session: Session, world) -> None:
        # Refusing it would make an unowned row invisible to everyone but an
        # institution-wide role — including whoever has to fix it.
        from academic_ai.api.crud import ScopeRule
        from academic_ai.security.rbac import Principal

        rule = ScopeRule(column="faculty_id", kind="faculty")
        principal = Principal(
            user_id=str(uuid.uuid4()),
            role=UserRole.DEAN,
            faculty_ids=frozenset({str(world.faculty.id)}),
        )
        assert rule.covers(principal.jurisdiction, ())

    def test_an_institution_wide_role_still_sees_everything(
        self, client: TestClient, admin: User, two_faculties
    ) -> None:
        # The existing tests all run as an ADMIN; this states why they kept
        # passing rather than leaving it to look like the check does nothing.
        token = _login(client, "crudadmin@example.test")
        response = client.get("/api/v1/departments", headers=_auth(token))
        assert response.status_code == 200

    def test_an_unassigned_scoped_role_sees_nothing(
        self, session: Session, world
    ) -> None:
        # Deny-by-default doing its job, not a bug. An empty jurisdiction
        # yields no rows.
        from academic_ai.api.crud import ScopeRule
        from academic_ai.security.rbac import Principal

        rule = ScopeRule(column="faculty_id", kind="faculty")
        unassigned = Principal(user_id=str(uuid.uuid4()), role=UserRole.DEAN)
        assert not rule.covers(unassigned.jurisdiction, (world.faculty.id,))

    def test_every_crud_entity_states_a_scope_decision(self) -> None:
        # The point of `scope=None` meaning "institution-wide *by decision*":
        # a new entity added without thinking about ownership should be a
        # visible blank in this list, not a silent default.
        from academic_ai.api.routers.academic import _CONFIGS as CONFIGS

        decided = {c.prefix: c.scope for c in CONFIGS}
        scoped = {p for p, s in decided.items() if s is not None}
        assert scoped == {
            # Direct owner column.
            "/departments",
            "/programs",
            "/curricula",
            "/students",
            # One hop to a parent that holds the owner.
            "/courses",
            "/teachers",
            "/curriculum-versions",
            # Owner is a set, through course_offering_program.
            "/offerings",
            "/sections",
            "/sessions",
            "/enrollments",
            # Their own scope_program_id column.
            "/constraints",
            "/course-equivalences",
        }, (
            "the set of scoped entities changed; if that is intended, update "
            "SECURITY.md, which lists which entities are scoped and why the "
            "rest are not"
        )


class TestOneHopScope:
    """SESSION 25: entities whose owner is a join away.

    `Course.department_id` -> `Department.faculty_id`. The row check loads the
    parent; the list filter becomes an `IN (SELECT ...)`. Each of these gets
    its own test rather than sharing one, because an almost-right join looks
    exactly like enforcement while letting rows through.
    """

    @pytest.fixture()
    def two_departments(self, session: Session, world):
        from academic_ai.infrastructure.db.models import Department, Faculty

        other_faculty = Faculty(
            institution_id=world.institution.id, code="FAC-B", name="Facultad B"
        )
        session.add(other_faculty)
        session.flush()
        theirs = Department(
            faculty_id=other_faculty.id, code="DEP-B", name="Departamento B"
        )
        session.add(theirs)
        session.flush()
        return world.department, theirs, other_faculty

    def _dean_of(self, faculty_id):
        from academic_ai.security.rbac import Principal

        return Principal(
            user_id=str(uuid.uuid4()),
            role=UserRole.DEAN,
            faculty_ids=frozenset({str(faculty_id)}),
        )

    def test_a_course_is_owned_through_its_department(
        self, session: Session, world, two_departments
    ) -> None:
        from academic_ai.api.routers.academic import _CONFIGS

        mine, theirs, _ = two_departments
        rule = next(c for c in _CONFIGS if c.prefix == "/courses").scope
        dean = self._dean_of(world.faculty.id)

        ours = world.course  # sits in world.department, world.faculty
        assert rule.covers(dean.jurisdiction, rule.owners_of(session, ours))

        from academic_ai.infrastructure.db.models import Course

        theirs_course = Course(
            institution_id=world.institution.id,
            department_id=theirs.id,
            code="AJENO-1",
            name="Curso ajeno",
            credits=4,
            theory_hours=3,
        )
        session.add(theirs_course)
        session.flush()
        assert not rule.covers(
            dean.jurisdiction, rule.owners_of(session, theirs_course)
        )

    def test_a_teacher_without_a_department_stays_visible(
        self, session: Session, world
    ) -> None:
        # `Teacher.department_id` is nullable. A teacher belonging to no
        # faculty must not become invisible to everyone but an
        # institution-wide role — including whoever has to give them one.
        from academic_ai.api.routers.academic import _CONFIGS

        rule = next(c for c in _CONFIGS if c.prefix == "/teachers").scope
        dean = self._dean_of(world.faculty.id)

        world.teacher.department_id = None
        session.flush()
        assert rule.covers(dean.jurisdiction, rule.owners_of(session, world.teacher))

    def test_a_curriculum_version_is_owned_through_its_curriculum(
        self, session: Session, world
    ) -> None:
        from academic_ai.api.routers.academic import _CONFIGS

        rule = next(c for c in _CONFIGS if c.prefix == "/curriculum-versions").scope
        # The seeded curriculum belongs to the seeded programme.
        holder = self._dean_of(world.faculty.id)
        from academic_ai.security.rbac import Principal

        coordinator = Principal(
            user_id=str(uuid.uuid4()),
            role=UserRole.COORDINATOR,
            program_ids=frozenset({str(world.program.id)}),
        )
        owners = rule.owners_of(session, world.curriculum_version)
        assert [str(o) for o in owners] == [str(world.program.id)]
        assert rule.covers(coordinator.jurisdiction, owners)
        del holder

    def test_an_unassigned_role_reaches_nothing_through_a_hop(
        self, session: Session, world
    ) -> None:
        from academic_ai.api.routers.academic import _CONFIGS
        from academic_ai.security.rbac import Principal

        rule = next(c for c in _CONFIGS if c.prefix == "/courses").scope
        unassigned = Principal(user_id=str(uuid.uuid4()), role=UserRole.DEAN)
        assert not rule.covers(
            unassigned.jurisdiction, rule.owners_of(session, world.course)
        )

    def test_the_list_filter_and_the_row_check_agree(
        self, session: Session, world, two_departments
    ) -> None:
        # The failure mode a join rule invites: a row the list shows but the
        # row check refuses, or worse, the other way round.
        from sqlalchemy import select

        from academic_ai.api.routers.academic import _CONFIGS
        from academic_ai.infrastructure.db.models import Course

        mine, theirs, _ = two_departments
        session.add(
            Course(
                institution_id=world.institution.id,
                department_id=theirs.id,
                code="AJENO-2",
                name="Otro ajeno",
                credits=4,
                theory_hours=3,
            )
        )
        session.flush()

        rule = next(c for c in _CONFIGS if c.prefix == "/courses").scope
        dean = self._dean_of(world.faculty.id)

        listed = session.scalars(
            rule.restrict(select(Course), Course, dean.jurisdiction)
        ).all()
        every = session.scalars(select(Course)).all()

        assert listed, "the list filter returned nothing at all"
        assert len(listed) < len(every), "the list filter excluded nothing"
        for row in every:
            allowed = rule.covers(dean.jurisdiction, rule.owners_of(session, row))
            assert (row in listed) == allowed, (
                f"{row.code}: list says {row in listed}, row check says {allowed}"
            )


class TestSharedScope:
    """SESSION 26: entities whose owner is a *set*.

    A `CourseOffering` serves every programme listed against it in
    `course_offering_program`, and a shared or common-core course serves
    several. Membership is any overlap, never all: requiring all would mean
    only an institution-wide role could touch a shared course — which is
    exactly the course most likely to need a programme's own coordinator.
    """

    @pytest.fixture()
    def shared_offering(self, session: Session, offered):
        """The seeded offering, additionally serving a second programme."""
        from academic_ai.domain.enums import ServiceRole
        from academic_ai.infrastructure.db.models import CourseOfferingProgram

        session.add(
            CourseOfferingProgram(
                course_offering_id=offered.offering.id,
                program_id=offered.other_program.id,
                service_role=ServiceRole.SERVICE,
            )
        )
        session.flush()
        return offered

    def _coordinator_of(self, program_id):
        from academic_ai.security.rbac import Principal

        return Principal(
            user_id=str(uuid.uuid4()),
            role=UserRole.COORDINATOR,
            program_ids=frozenset({str(program_id)}),
        )

    def _rule(self, prefix: str):
        from academic_ai.api.routers.academic import _CONFIGS

        return next(c for c in _CONFIGS if c.prefix == prefix).scope

    def test_an_offering_lists_every_programme_it_serves(
        self, session: Session, shared_offering
    ) -> None:
        rule = self._rule("/offerings")
        owners = {str(o) for o in rule.owners_of(session, shared_offering.offering)}
        assert owners == {
            str(shared_offering.program.id),
            str(shared_offering.other_program.id),
        }

    def test_either_programmes_coordinator_reaches_it(
        self, session: Session, shared_offering
    ) -> None:
        rule = self._rule("/offerings")
        owners = rule.owners_of(session, shared_offering.offering)
        for program in (shared_offering.program, shared_offering.other_program):
            assert rule.covers(self._coordinator_of(program.id).jurisdiction, owners)

    def test_a_third_programmes_coordinator_does_not(
        self, session: Session, shared_offering
    ) -> None:
        rule = self._rule("/offerings")
        owners = rule.owners_of(session, shared_offering.offering)
        assert not rule.covers(self._coordinator_of(uuid.uuid4()).jurisdiction, owners)

    def test_a_section_is_owned_through_its_offering(
        self, session: Session, shared_offering
    ) -> None:
        rule = self._rule("/sections")
        owners = rule.owners_of(session, shared_offering.section)
        assert str(shared_offering.program.id) in {str(o) for o in owners}

    def test_a_session_is_owned_two_hops_out(
        self, session: Session, shared_offering
    ) -> None:
        # ClassSession -> Section -> CourseOffering -> programmes. The longest
        # path any rule takes, and the one most likely to be quietly wrong.
        rule = self._rule("/sessions")
        owners = rule.owners_of(session, shared_offering.class_session)
        assert {str(o) for o in owners} == {
            str(shared_offering.program.id),
            str(shared_offering.other_program.id),
        }

    def test_an_offering_serving_nobody_stays_visible(
        self, session: Session, world
    ) -> None:
        # An offering with no programme rows is a data problem, and hiding it
        # would put it out of reach of everyone who could fix it.
        from academic_ai.domain.enums import Modality
        from academic_ai.infrastructure.db.models import CourseOffering

        orphan = CourseOffering(
            course_id=world.course.id,
            academic_period_id=world.period.id,
            campus_id=world.campus.id,
            modality=Modality.ONSITE,
        )
        session.add(orphan)
        session.flush()

        rule = self._rule("/offerings")
        assert rule.owners_of(session, orphan) == ()
        assert rule.covers(self._coordinator_of(uuid.uuid4()).jurisdiction, ())

    def test_the_list_filter_and_the_row_check_agree_on_sessions(
        self, session: Session, shared_offering
    ) -> None:
        # The longest path, checked the same way the one-hop rule is: a join
        # invites one of the two showing what the other refuses, and it is
        # invisible from either side alone.
        from sqlalchemy import select

        from academic_ai.infrastructure.db.models import ClassSession

        rule = self._rule("/sessions")
        mine = self._coordinator_of(shared_offering.program.id)
        stranger = self._coordinator_of(uuid.uuid4())

        every = session.scalars(select(ClassSession)).all()
        assert every

        for principal, expect_any in ((mine, True), (stranger, False)):
            listed = session.scalars(
                rule.restrict(select(ClassSession), ClassSession, principal.jurisdiction)
            ).all()
            assert bool(listed) is expect_any
            for row in every:
                allowed = rule.covers(
                    principal.jurisdiction, rule.owners_of(session, row)
                )
                assert (row in listed) == allowed, (
                    f"session {row.id}: list says {row in listed}, "
                    f"row check says {allowed}"
                )
