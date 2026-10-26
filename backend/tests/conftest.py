"""Test fixtures.

Tests run against a **real PostgreSQL** instance, never SQLite. Most of what
SESSION 1 actually guarantees — partial unique indexes, GiST exclusion
constraints, ``NULLS NOT DISTINCT``, ``num_nonnulls`` checks, plpgsql
triggers — does not exist in SQLite, so a SQLite test suite would pass while
proving nothing about the schema that ships.

Point ``TEST_DATABASE_URL`` (or ``DATABASE_URL``) at a throwaway database.
The default matches the project's loopback-only dev instance from ADR-0002.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from academic_ai.domain.enums import (
    AcademicPeriodStatus,
    CourseStatus,
    DayOfWeek,
    LifecycleStatus,
    Modality,
    OfferingStatus,
    RequirementType,
    RoomType,
    ScheduleScope,
    ScheduleVersionStatus,
    SectionStatus,
    ServiceRole,
    SessionType,
    TeacherContractType,
    TeacherRole,
    UserRole,
)
from academic_ai.infrastructure.db.base import Base
from academic_ai.infrastructure.db.models import (
    AcademicPeriod,
    Building,
    Campus,
    ClassSession,
    Course,
    CourseOffering,
    CourseOfferingProgram,
    Curriculum,
    CurriculumCourse,
    CurriculumLevel,
    CurriculumVersion,
    Department,
    Faculty,
    Institution,
    Program,
    Room,
    Schedule,
    ScheduleVersion,
    Section,
    SessionTeacher,
    Student,
    Teacher,
    Timeslot,
    User,
)

DEFAULT_TEST_URL = (
    "postgresql+psycopg://academic_ai_app:dev_only_not_a_secret"
    "@127.0.0.1:5434/academic_ai_test"
)


def _test_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_URL


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """Engine against a freshly built test database.

    The schema is created from ``Base.metadata`` rather than by running
    Alembic, so a broken migration cannot silently take the whole suite down
    with it. `tests/test_migrations.py` checks the migration separately, and
    that separation is deliberate: it tells you *which* of the two is wrong.
    """
    url = _test_database_url()
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    db_name = url.rsplit("/", 1)[1]

    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        try:
            conn = admin.connect()
        except Exception as exc:  # pragma: no cover - environment problem
            pytest.skip(f"PostgreSQL is not available for tests: {exc}")
        # Reaching here means the server answered. A failure *now* is this
        # database being unavailable, not the server — and the usual cause is
        # a second run of this suite holding it, because the name is fixed and
        # every run drops and recreates it. That must not become a skip:
        # skipping is how a concurrent run goes green having tested nothing,
        # which is the one outcome worse than failing.
        with conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        admin.dispose()

    # `pool_pre_ping` because the suite drops and recreates databases while
    # this session-scoped pool is alive: `test_migration.py` runs
    # `DROP DATABASE ... WITH (FORCE)` between the slow LNS tests and the next
    # database test, and a connection that was checked back into the pool
    # before that is dead by the time it is handed out again. The symptom is
    # an `AdminShutdown` in whichever fixture happens to be first afterwards —
    # `test_offering_hierarchy`'s `world`, deterministically — which looks
    # like a bug in that test and is not one. Pre-ping validates and replaces
    # a dead connection instead.
    eng = create_engine(url, pool_pre_ping=True)
    with eng.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS btree_gist"))
        conn.commit()

    Base.metadata.create_all(eng)
    _create_integrity_triggers(eng)

    yield eng

    eng.dispose()
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)'))
    admin.dispose()


def _create_integrity_triggers(eng: Engine) -> None:
    """Install the same triggers the migration installs.

    They are not expressible in ``Base.metadata``, so a metadata-built schema
    would otherwise lack them and the trigger tests would vacuously pass.
    The SQL is read from the migration module so the two cannot drift.
    """
    from tests._migration_sql import trigger_sql

    with eng.connect() as conn:
        conn.execute(text(trigger_sql()))
        conn.commit()


@pytest.fixture()
def session(engine: Engine) -> Iterator[Session]:
    """A session whose work is rolled back after each test.

    The outer transaction is never committed, so tests are isolated without
    paying to rebuild the schema between them. Tests that need to observe a
    constraint firing use ``session.flush()``; the rollback still cleans up.
    """
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, expire_on_commit=False)
    db = factory()

    try:
        yield db
    finally:
        db.close()
        # A constraint that fires while flushing a *dirty* object leaves the
        # outer transaction already deassociated; rolling back again would
        # only emit a warning.
        if transaction.is_active:
            transaction.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# Deterministic academic fixture
#
# One small but complete institution. Every test that needs "a course in a
# period on a campus" builds on this rather than inventing its own, so a
# schema change breaks one fixture instead of forty tests.
# ---------------------------------------------------------------------------


class AcademicWorld:
    """Handles to the seeded entities, for readable tests."""

    def __init__(self, **kwargs: object) -> None:
        self.__dict__.update(kwargs)


@pytest.fixture()
def world(session: Session) -> AcademicWorld:
    institution = Institution(code="UNI", name="Universidad de Prueba")
    session.add(institution)
    session.flush()

    admin = User(
        email="admin@example.test",
        full_name="Admin User",
        primary_role=UserRole.ADMIN,
    )
    session.add(admin)

    faculty = Faculty(institution_id=institution.id, code="ING", name="Ingeniería")
    session.add(faculty)
    session.flush()

    department = Department(
        faculty_id=faculty.id, code="MAT", name="Matemáticas"
    )
    program = Program(
        faculty_id=faculty.id, code="SIS", name="Ingeniería en Sistemas"
    )
    other_program = Program(
        faculty_id=faculty.id, code="IND", name="Ingeniería Industrial"
    )
    session.add_all([department, program, other_program])
    session.flush()

    campus = Campus(institution_id=institution.id, code="CENTRAL", name="Campus Central")
    session.add(campus)
    session.flush()

    building = Building(campus_id=campus.id, code="A", name="Bloque A")
    session.add(building)
    session.flush()

    room = Room(
        building_id=building.id,
        campus_id=campus.id,
        code="A101",
        name="Aula A101",
        room_type=RoomType.CLASSROOM,
        capacity=40,
    )
    session.add(room)

    period = AcademicPeriod(
        institution_id=institution.id,
        code="2026-1",
        name="Periodo 2026-1",
        start_date=dt.date(2026, 3, 1),
        end_date=dt.date(2026, 7, 31),
        slot_minutes=60,
    )
    session.add(period)
    session.flush()

    # A dense one-day grid: 08:00-13:00, with 12:00-13:00 blocked for lunch.
    timeslots = []
    for index, start in enumerate(range(8 * 60, 13 * 60, 60)):
        slot = Timeslot(
            academic_period_id=period.id,
            day_of_week=DayOfWeek.MONDAY,
            start_minute=start,
            end_minute=start + 60,
            day_slot_index=index,
            global_slot_index=index,
            is_schedulable=start != 12 * 60,
        )
        timeslots.append(slot)
    session.add_all(timeslots)

    teacher = Teacher(
        institution_id=institution.id,
        department_id=department.id,
        code="T001",
        full_name="Ada Lovelace",
        contract_type=TeacherContractType.FULL_TIME,
        max_weekly_hours=40,
    )
    session.add(teacher)

    course = Course(
        institution_id=institution.id,
        department_id=department.id,
        code="MAT101",
        name="Cálculo I",
        credits=4,
        theory_hours=4,
        status=CourseStatus.ACTIVE,
    )
    other_course = Course(
        institution_id=institution.id,
        department_id=department.id,
        code="MAT150",
        name="Cálculo Diferencial",
        credits=3,
        theory_hours=3,
        status=CourseStatus.ACTIVE,
    )
    session.add_all([course, other_course])
    session.flush()

    curriculum = Curriculum(program_id=program.id, code="SIS-2020", name="Malla 2020")
    session.add(curriculum)
    session.flush()

    curriculum_version = CurriculumVersion(
        curriculum_id=curriculum.id,
        version_number=1,
        valid_from=dt.date(2020, 1, 1),
        status=LifecycleStatus.ACTIVE,
    )
    session.add(curriculum_version)
    session.flush()

    level = CurriculumLevel(
        curriculum_version_id=curriculum_version.id, level_number=1, name="Nivel 1"
    )
    session.add(level)
    session.flush()

    curriculum_course = CurriculumCourse(
        curriculum_version_id=curriculum_version.id,
        curriculum_level_id=level.id,
        course_id=course.id,
        requirement_type=RequirementType.MANDATORY,
    )
    session.add(curriculum_course)

    student = Student(
        institution_id=institution.id,
        program_id=program.id,
        curriculum_version_id=curriculum_version.id,
        code="S001",
        full_name="Grace Hopper",
    )
    session.add(student)
    session.flush()

    return AcademicWorld(
        institution=institution,
        admin=admin,
        faculty=faculty,
        department=department,
        program=program,
        other_program=other_program,
        campus=campus,
        building=building,
        room=room,
        period=period,
        timeslots=timeslots,
        teacher=teacher,
        course=course,
        other_course=other_course,
        curriculum=curriculum,
        curriculum_version=curriculum_version,
        level=level,
        curriculum_course=curriculum_course,
        student=student,
        modality=Modality.ONSITE,
        period_status=AcademicPeriodStatus.PLANNING,
        new_uuid=lambda: uuid.uuid4(),
    )


@pytest.fixture()
def offered(session: Session, world: AcademicWorld) -> AcademicWorld:
    """`world`, plus one offering with one section and one 2-slot session.

    This is the smallest complete thing that can be scheduled, and it is what
    the enrollment, schedule and criticality tests build on.
    """
    offering = CourseOffering(
        course_id=world.course.id,
        academic_period_id=world.period.id,
        campus_id=world.campus.id,
        modality=Modality.ONSITE,
        status=OfferingStatus.OPEN,
    )
    session.add(offering)
    session.flush()

    session.add(
        CourseOfferingProgram(
            course_offering_id=offering.id,
            program_id=world.program.id,
            service_role=ServiceRole.CORE,
        )
    )

    section = Section(
        course_offering_id=offering.id,
        academic_period_id=world.period.id,
        code="A",
        capacity=2,
        status=SectionStatus.OPEN,
    )
    session.add(section)
    session.flush()

    class_session = ClassSession(
        section_id=section.id,
        academic_period_id=world.period.id,
        sequence_number=1,
        session_type=SessionType.LECTURE,
        delivery_mode=Modality.ONSITE,
        duration_slots=2,
        requires_timeslot=True,
        requires_room=True,
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

    schedule = Schedule(
        institution_id=world.institution.id,
        academic_period_id=world.period.id,
        scope=ScheduleScope.INSTITUTION,
        code="MAIN",
        name="Horario institucional",
    )
    session.add(schedule)
    session.flush()

    version = ScheduleVersion(
        schedule_id=schedule.id,
        institution_id=world.institution.id,
        academic_period_id=world.period.id,
        version_number=1,
        status=ScheduleVersionStatus.DRAFT,
    )
    session.add(version)
    session.flush()

    world.__dict__.update(
        offering=offering,
        section=section,
        class_session=class_session,
        schedule=schedule,
        version=version,
    )
    return world
