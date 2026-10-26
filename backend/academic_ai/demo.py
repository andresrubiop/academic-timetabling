"""A complete, small period to look at. ``python -m academic_ai.demo``.

The platform can be deployed, healthy and signed into, and still show nothing:
`GET /schedule` answers 404 "no class sessions to schedule" for an empty
period, which is correct and unhelpful. This builds the smallest institution
that is worth looking at — one that loads, solves, and has enough overlap for
the impact analysis to have something to say.

**Sample data, and it says so.** Every code is prefixed `DEMO-` and the
institution is named as such, so nobody has to guess later whether a row came
from here or from a registrar. The command refuses to run twice, and refuses
to run at all against a database that already holds a non-demo institution:
seeding sample courses into a real deployment is the kind of mistake that is
discovered a term later.
"""

from __future__ import annotations

import datetime as dt
import random
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import func, or_, select

DEMO_PREFIX = "DEMO"

#: Small enough to read on one screen, large enough that the optimizer has
#: real choices to make and two programmes genuinely share a course.
DAYS = ("MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY")
SLOTS_PER_DAY = 6
FIRST_SLOT_MINUTE = 7 * 60  # 07:00
SLOT_MINUTES = 60


@dataclass(frozen=True, slots=True)
class DemoResult:
    institution_id: uuid.UUID
    period_id: uuid.UUID
    sessions: int
    students: int
    #: The published version the grid renders, when the demo solved one.
    schedule_version_id: uuid.UUID | None = None
    solver_status: str = "not solved"


def _refuse_if_unsafe(session) -> None:
    from academic_ai.infrastructure.db.models import Institution

    rows = session.scalars(select(Institution)).all()
    demo = [i for i in rows if i.code.startswith(DEMO_PREFIX)]
    if demo:
        raise SystemExit(
            "demo: the demo institution already exists. Delete it first if you "
            "want a fresh one; this command does not merge into an existing "
            "period."
        )
    if rows:
        raise SystemExit(
            f"demo: this database already holds {len(rows)} institution(s) that "
            "are not demo data. Refusing — seeding sample courses into a real "
            "deployment is discovered a term later, not immediately."
        )


def seed_approval_chains(
    session, institution_id: uuid.UUID, prefix: str = DEMO_PREFIX
) -> int:
    """Configure approval chains for a seeded institution.

    Not a fallback, and the distinction matters. SESSION 12 refuses to route a
    change when no workflow is configured, because "a change routed to the
    wrong authorities looks exactly like one routed to the right ones" — so
    the code must never invent a chain at runtime. This is the other thing:
    explicit configuration, written once, for an institution these commands
    created.

    Without it a seeded period can propose a change and never submit one: the
    router refuses with "no approval workflow is configured for PROGRAM
    scope". A real institution configures its own, and there is currently no
    screen for that.
    """
    from academic_ai.domain.enums import ScheduleScope, UserRole
    from academic_ai.infrastructure.db.models import ApprovalStep, ApprovalWorkflow

    # One step per scope, widening with the reach of the change: a programme's
    # own coordinator for a change inside it, a dean above a faculty, an
    # administrator once it crosses faculties.
    chains = (
        (ScheduleScope.PROGRAM, (UserRole.COORDINATOR,)),
        (ScheduleScope.FACULTY, (UserRole.COORDINATOR, UserRole.DEAN)),
        (
            ScheduleScope.INSTITUTION,
            (UserRole.COORDINATOR, UserRole.DEAN, UserRole.ADMIN),
        ),
    )
    made = 0
    for scope, roles in chains:
        workflow = ApprovalWorkflow(
            institution_id=institution_id,
            code=f"{prefix}-{scope.value}"[:32],
            name=f"Cadena para cambios de alcance {scope.value}",
            applies_to_scope=scope,
            is_active=True,
        )
        session.add(workflow)
        session.flush()
        for number, role in enumerate(roles, start=1):
            session.add(
                ApprovalStep(
                    approval_workflow_id=workflow.id,
                    step_number=number,
                    required_role=role,
                    # Every coordinator of an affected programme signs the
                    # first step; the wider roles sign once each.
                    requires_all_affected=(role is UserRole.COORDINATOR),
                    description=f"Firma de {role.value}",
                )
            )
        session.flush()
        made += 1
    return made


def _an_administrator(session) -> uuid.UUID:
    """Somebody to publish as, or a refusal that says what to do."""
    from academic_ai.domain.enums import UserRole
    from academic_ai.infrastructure.db.models import User

    user = session.scalars(
        select(User)
        .where(User.primary_role.in_((UserRole.SUPER_ADMIN, UserRole.ADMIN)))
        .order_by(User.created_at)
    ).first()
    if user is None:
        raise SystemExit(
            "demo: no administrator exists, and a published timetable must "
            "name who published it. Run `python -m academic_ai.bootstrap` "
            "first."
        )
    return user.id


#: Deletion order for `--reset`, **derived from the schema** rather than
#: written out.
#:
#: The hand-written list this replaces missed eleven tables — change requests,
#: approvals, scenarios, optimization runs, recommendations, cohorts,
#: preferences — so a reset failed the moment a seeded institution had been
#: used for anything. The order now comes from SQLAlchemy's own dependency
#: sort, reversed, and is filtered to what can actually reach an institution:
#: a table added later is included automatically, in the right place, without
#: anybody remembering to.
#:
#: The foreign keys still refuse the convenient order, which is why this
#: exists at all — an institution cannot be deleted while a period references
#: it.
def _deletion_order() -> tuple[str, ...]:
    from academic_ai.infrastructure.db.base import Base
    import academic_ai.infrastructure.db.models  # noqa: F401

    tables = list(Base.metadata.sorted_tables)
    parents = {
        t.name: {fk.column.table.name for fk in t.foreign_keys} for t in tables
    }
    reachable = {"institution"}
    changed = True
    while changed:
        changed = False
        for name, ancestors in parents.items():
            if name not in reachable and ancestors & reachable:
                reachable.add(name)
                changed = True
    return tuple(t.name for t in reversed(tables) if t.name in reachable)


def reset(session, prefix: str = DEMO_PREFIX) -> int:
    """Remove every row of one seeded institution.

    Scoped to the institution and nothing else. The rows are found by walking
    *down* the foreign keys from it — a row belongs when any of its keys
    points at something already collected — and then deleted in reverse
    dependency order.

    **Uniform on purpose.** This replaces a hand-written table list and a
    chain of hand-written `WHERE` clauses, which between them missed eleven
    tables: change requests, approvals, scenarios, optimization runs,
    recommendations, cohorts, preferences. A reset therefore failed the moment
    a seeded institution had been *used* for anything, which is exactly when
    somebody wants to re-seed it. Deriving both the order and the scope from
    the schema means a table added later is handled without anybody
    remembering to.

    **Published versions are moved back to DRAFT first.** Two protections
    stand in the way and both are right: `schedule_version_is_deletable`
    (SESSION 13) refuses to delete anything but a DRAFT, and
    `schedule_assignment_version_is_mutable` refuses to touch the assignments
    of a PUBLISHED *or* ARCHIVED version — so archiving satisfies the first
    and keeps failing the second. This is the one place that walks a published
    version backwards, and it is defensible only because it is a teardown of
    an institution these seeding commands created.
    """
    from sqlalchemy import text

    from academic_ai.domain.enums import ScheduleVersionStatus
    from academic_ai.infrastructure.db.base import Base
    from academic_ai.infrastructure.db.models import Institution, ScheduleVersion
    import academic_ai.infrastructure.db.models  # noqa: F401

    roots = session.scalars(
        select(Institution).where(Institution.code.like(f"{prefix}%"))
    ).all()
    if not roots:
        return 0

    for version in session.scalars(
        select(ScheduleVersion).where(
            ScheduleVersion.institution_id.in_([i.id for i in roots])
        )
    ):
        if version.status is not ScheduleVersionStatus.DRAFT:
            version.status = ScheduleVersionStatus.DRAFT
    session.flush()

    tables = list(Base.metadata.sorted_tables)
    order = _deletion_order()

    # Walk down: a row belongs to this institution when any foreign key of its
    # own points at a row already known to belong.
    owned: dict[str, list] = {"institution": [i.id for i in roots]}
    for table in tables:
        if table.name not in order or table.name == "institution":
            continue
        clauses = []
        for fk in table.foreign_keys:
            # Only references to a parent's identity. Composite keys like
            # `schedule_assignment (class_session_id, day_of_week)` appear here
            # one column at a time, and following the second one compares a
            # day name against a uuid.
            if fk.column.name != "id":
                continue
            parent = fk.column.table.name
            ids = owned.get(parent)
            if not ids:
                continue
            clauses.append(fk.parent.in_(ids))
        if not clauses:
            continue
        found = session.execute(
            select(table.c.id).where(or_(*clauses))
        ).scalars().all()
        if found:
            owned[table.name] = found

    # Break the cycles before deleting. `schedule_version` and
    # `optimization_run` reference each other, so no ordering of the two can
    # be right — SQLAlchemy's dependency sort breaks the tie arbitrarily and
    # whichever way it falls, one deletion is refused. Both of the keys
    # involved are nullable, which is what makes the cycle resolvable at all:
    # setting them to NULL first removes the reference without removing the
    # fact of it, because the row that carries the fact is about to go too.
    position = {name: index for index, name in enumerate(order)}
    for table in tables:
        if table.name not in owned:
            continue
        for fk in table.foreign_keys:
            if fk.column.name != "id" or fk.parent.nullable is False:
                continue
            parent = fk.column.table.name
            if parent in position and position[parent] < position[table.name]:
                session.execute(
                    text(
                        f"UPDATE {table.name} SET {fk.parent.name} = NULL "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": [str(i) for i in owned[table.name]]},
                )
    session.flush()

    for name in order:
        ids = owned.get(name)
        if not ids:
            continue
        session.execute(
            text(f"DELETE FROM {name} WHERE id = ANY(:ids)"),
            {"ids": [str(i) for i in ids]},
        )
    session.flush()
    return sum(1 for name in order if owned.get(name))


def build(session, *, seed: int = 20260826) -> DemoResult:
    """Create the demo institution. Returns the ids worth printing."""
    from academic_ai.domain.enums import (
        AcademicPeriodStatus,
        CourseStatus,
        DayOfWeek,
        EnrollmentKind,
        EnrollmentStatus,
        LifecycleStatus,
        Modality,
        OfferingStatus,
        RequirementType,
        RoomType,
        SectionStatus,
        ServiceRole,
        SessionType,
        TeacherContractType,
        TeacherRole,
    )
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
        Enrollment,
        Faculty,
        Institution,
        Program,
        Room,
        Section,
        SessionTeacher,
        Student,
        Teacher,
        Timeslot,
    )

    rng = random.Random(seed)

    institution = Institution(code=f"{DEMO_PREFIX}-UNI", name="Universidad Demo")
    session.add(institution)
    session.flush()

    faculty = Faculty(
        institution_id=institution.id,
        code=f"{DEMO_PREFIX}-ING",
        name="Facultad de Ingeniería",
    )
    session.add(faculty)
    session.flush()

    department = Department(
        faculty_id=faculty.id,
        code=f"{DEMO_PREFIX}-DSIS",
        name="Departamento de Sistemas",
    )
    session.add(department)
    session.flush()

    # Two programmes, so a shared course has somewhere to be shared *to* —
    # which is what makes the scope rules and the approval routing do
    # anything visible.
    programs = []
    for code, name in (
        (f"{DEMO_PREFIX}-SIS", "Ingeniería en Sistemas"),
        (f"{DEMO_PREFIX}-IND", "Ingeniería Industrial"),
    ):
        program = Program(
            faculty_id=faculty.id,
            department_id=department.id,
            code=code,
            name=name,
            degree_title=name,
        )
        session.add(program)
        session.flush()
        programs.append(program)

    campus = Campus(
        institution_id=institution.id, code=f"{DEMO_PREFIX}-CENTRO", name="Campus Centro"
    )
    session.add(campus)
    session.flush()
    building = Building(
        campus_id=campus.id, code=f"{DEMO_PREFIX}-A", name="Edificio A"
    )
    session.add(building)
    session.flush()

    rooms = []
    for index, (capacity, kind) in enumerate(
        ((40, RoomType.CLASSROOM), (40, RoomType.CLASSROOM), (30, RoomType.COMPUTER_LAB)), 1
    ):
        room = Room(
            building_id=building.id,
            campus_id=campus.id,
            code=f"{DEMO_PREFIX}-A{index:02d}",
            name=f"Aula A{index:02d}",
            room_type=kind,
            capacity=capacity,
        )
        session.add(room)
        rooms.append(room)
    session.flush()

    period = AcademicPeriod(
        institution_id=institution.id,
        code=f"{DEMO_PREFIX}-2026-1",
        name="Periodo 2026-1",
        start_date=dt.date(2026, 3, 2),
        end_date=dt.date(2026, 7, 24),
        status=AcademicPeriodStatus.PLANNING,
    )
    session.add(period)
    session.flush()

    global_index = 0
    for day in DAYS:
        for slot in range(SLOTS_PER_DAY):
            start = FIRST_SLOT_MINUTE + slot * SLOT_MINUTES
            session.add(
                Timeslot(
                    academic_period_id=period.id,
                    day_of_week=DayOfWeek[day],
                    start_minute=start,
                    end_minute=start + SLOT_MINUTES,
                    day_slot_index=slot,
                    global_slot_index=global_index,
                    is_schedulable=True,
                    label=f"{start // 60:02d}:00",
                )
            )
            global_index += 1
    session.flush()

    teachers = []
    for index, name in enumerate(
        (
            "Ana Torres",
            "Luis Mendoza",
            "Carla Ruiz",
            "Jorge Salas",
            "María Vega",
            "Pedro Cifuentes",
        ),
        1,
    ):
        teacher = Teacher(
            institution_id=institution.id,
            department_id=department.id,
            code=f"{DEMO_PREFIX}-D{index:02d}",
            full_name=name,
            contract_type=TeacherContractType.FULL_TIME,
            max_weekly_hours=20,
            max_daily_hours=6,
        )
        session.add(teacher)
        teachers.append(teacher)
    session.flush()

    # One curriculum per programme, so enrolments have something to bind to.
    curricula = {}
    for program in programs:
        curriculum = Curriculum(
            program_id=program.id,
            code=f"{program.code}-MALLA",
            name=f"Malla {program.name}",
        )
        session.add(curriculum)
        session.flush()
        version = CurriculumVersion(
            curriculum_id=curriculum.id,
            version_number=1,
            status=LifecycleStatus.ACTIVE,
            valid_from=dt.date(2026, 1, 1),
        )
        session.add(version)
        session.flush()
        level = CurriculumLevel(
            curriculum_version_id=version.id, level_number=1, name="Primer nivel"
        )
        session.add(level)
        session.flush()
        curricula[program.id] = (curriculum, version, level)

    # Eight courses. The first is shared by both programmes -- the common-core
    # case that the scope rules and the approval routing exist for.
    course_specs = [
        ("MAT101", "Cálculo I", True),
        ("FIS101", "Física I", False),
        ("PRG101", "Programación I", False),
        ("EST201", "Estadística", False),
        ("BDD301", "Bases de Datos", False),
        ("RED301", "Redes", False),
        ("OPI201", "Investigación de Operaciones", False),
        ("CAL201", "Gestión de Calidad", False),
    ]
    courses = []
    for code, name, _shared in course_specs:
        course = Course(
            institution_id=institution.id,
            department_id=department.id,
            code=f"{DEMO_PREFIX}-{code}",
            name=name,
            credits=4,
            theory_hours=4,
            status=CourseStatus.ACTIVE,
        )
        session.add(course)
        courses.append(course)
    session.flush()

    sessions_created = 0
    offerings: list[tuple[CourseOffering, Section, list[ClassSession], Program]] = []
    for index, (course, (code, name, shared)) in enumerate(zip(courses, course_specs)):
        owning_program = programs[index % len(programs)]
        offering = CourseOffering(
            course_id=course.id,
            academic_period_id=period.id,
            campus_id=campus.id,
            modality=Modality.ONSITE,
            status=OfferingStatus.OPEN,
        )
        session.add(offering)
        session.flush()

        serving = [owning_program] + ([p for p in programs if p is not owning_program] if shared else [])
        for program in serving:
            session.add(
                CourseOfferingProgram(
                    course_offering_id=offering.id,
                    program_id=program.id,
                    service_role=(
                        ServiceRole.CORE if program is owning_program else ServiceRole.SERVICE
                    ),
                )
            )
        session.flush()

        section = Section(
            course_offering_id=offering.id,
            academic_period_id=period.id,
            code="A",
            capacity=30,
            status=SectionStatus.OPEN,
        )
        session.add(section)
        session.flush()

        made = []
        # Two meetings a week each: enough for the grid to look like a
        # timetable rather than a list.
        for sequence in (1, 2):
            class_session = ClassSession(
                section_id=section.id,
                academic_period_id=period.id,
                sequence_number=sequence,
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
                    teacher_id=teachers[index % len(teachers)].id,
                    role=TeacherRole.PRIMARY,
                )
            )
            made.append(class_session)
            sessions_created += 1
        session.flush()

        # Bind the course to the owning programme's curriculum, so an
        # enrolment can name a curriculum course.
        curriculum, version, level = curricula[owning_program.id]
        session.add(
            CurriculumCourse(
                curriculum_version_id=version.id,
                curriculum_level_id=level.id,
                course_id=course.id,
                requirement_type=RequirementType.MANDATORY,
            )
        )
        session.flush()
        offerings.append((offering, section, made, owning_program))

    # Students, each enrolled in several courses of their programme -- the
    # overlap is what gives the conflict analysis anything to measure.
    students = []
    for index in range(1, 41):
        program = programs[index % len(programs)]
        student = Student(
            institution_id=institution.id,
            program_id=program.id,
            code=f"{DEMO_PREFIX}-E{index:04d}",
            full_name=f"Estudiante {index:02d}",
        )
        session.add(student)
        students.append((student, program))
    session.flush()

    by_program: dict[uuid.UUID, list] = {}
    for offering, section, _made, owning in offerings:
        by_program.setdefault(owning.id, []).append((offering, section, owning))

    for student, program in students:
        available = by_program.get(program.id, [])
        chosen = rng.sample(available, k=min(3, len(available)))
        curriculum, version, level = curricula[program.id]
        for offering, section, owning in chosen:
            course_id = session.get(CourseOffering, offering.id).course_id
            curriculum_course = session.scalars(
                select(CurriculumCourse)
                .where(CurriculumCourse.curriculum_version_id == version.id)
                .where(CurriculumCourse.course_id == course_id)
            ).first()
            if curriculum_course is None:
                continue
            session.add(
                Enrollment(
                    student_id=student.id,
                    course_offering_id=offering.id,
                    section_id=section.id,
                    course_id=course_id,
                    academic_period_id=period.id,
                    curriculum_version_id=version.id,
                    curriculum_course_id=curriculum_course.id,
                    is_mandatory=True,
                    enrollment_kind=EnrollmentKind.CURRENT_LEVEL,
                    attempt_number=1,
                    status=EnrollmentStatus.ENROLLED,
                )
            )
    session.flush()

    return DemoResult(
        institution_id=institution.id,
        period_id=period.id,
        sessions=sessions_created,
        students=len(students),
    )


def solve_and_publish(
    session,
    period_id: uuid.UUID,
    *,
    published_by: uuid.UUID | None = None,
    seconds: int = 30,
) -> tuple[uuid.UUID | None, str]:
    """Solve the demo period and publish the result.

    Without this the grid is empty: `GET /schedule` reads its placement from
    the *published* version, and a period with no version has nothing placed.
    Sixteen sessions with nowhere to be is a correct answer and a useless
    demonstration.

    Published rather than left as a draft, deliberately. A draft is never
    picked up by recency (`snapshot.py`), which is the right rule and would
    mean the demo still showed nothing. Publishing also exercises the
    protections around a published version, which is worth seeing work.

    ``published_by`` is required by the schema, not by this function:
    `ck_schedule_version_published_requires_publisher` refuses a published
    version with nobody's name on it. That is the human-authority rule made
    structural — a timetable is published *by somebody* — and it means the
    demo cannot run before an administrator exists.
    """
    from academic_ai.domain.enums import ScheduleScope, ScheduleVersionStatus
    from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
    from academic_ai.domain.scheduling.problem import ProblemInstance
    from academic_ai.infrastructure.db.models import (
        AcademicPeriod,
        Schedule,
        ScheduleAssignment,
        ScheduleVersion,
        Timeslot,
    )
    from academic_ai.infrastructure.repositories import load_constraints, load_snapshot
    from academic_ai.optimization.solve import SolverConfig, solve

    snapshot, _diagnostics = load_snapshot(session, period_id)
    instance = ProblemInstance(
        snapshot=snapshot,
        constraints=load_constraints(session, period_id),
        objective_profile=OBJECTIVE_PROFILES["GREENFIELD"],
    )
    outcome = solve(instance, SolverConfig(time_limit_seconds=seconds))
    if not outcome.is_solved:
        return None, outcome.solver_status.value

    if published_by is None:
        published_by = _an_administrator(session)

    period = session.get(AcademicPeriod, period_id)
    schedule = Schedule(
        institution_id=period.institution_id,
        academic_period_id=period_id,
        scope=ScheduleScope.INSTITUTION,
        code=f"{DEMO_PREFIX}-MAIN",
        name="Horario institucional (demo)",
    )
    session.add(schedule)
    session.flush()

    # DRAFT first. `schedule_assignment_version_is_mutable()` refuses to add
    # an assignment to a version that is already PUBLISHED -- "create a new
    # version instead" -- which is exactly the invariant that makes a
    # published timetable something students can rely on. Build it, then
    # publish it, in that order.
    version = ScheduleVersion(
        schedule_id=schedule.id,
        institution_id=period.institution_id,
        academic_period_id=period_id,
        version_number=1,
        status=ScheduleVersionStatus.DRAFT,
    )
    session.add(version)
    session.flush()

    slots = {
        str(t.id): t
        for t in session.scalars(
            select(Timeslot).where(Timeslot.academic_period_id == period_id)
        )
    }
    by_session = {s.id: s for s in snapshot.sessions}
    for placed in outcome.assignments:
        slot = slots[placed.start_slot_id]
        spec = by_session[placed.session_id]
        session.add(
            ScheduleAssignment(
                schedule_version_id=version.id,
                class_session_id=uuid.UUID(placed.session_id),
                academic_period_id=period_id,
                start_timeslot_id=slot.id,
                day_of_week=slot.day_of_week,
                start_slot_index=slot.day_slot_index,
                duration_slots=spec.duration_slots,
                requires_room=spec.requires_room,
                room_id=uuid.UUID(placed.room_id) if placed.room_id else None,
            )
        )
    session.flush()

    version.status = ScheduleVersionStatus.PUBLISHED
    version.published_at = dt.datetime.now(dt.timezone.utc)
    version.published_by_user_id = published_by
    session.flush()
    return version.id, outcome.solver_status.value


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m academic_ai.demo")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the existing demo data first. Touches only rows that "
        "belong to the demo institution.",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    from academic_ai.infrastructure.db.session import session_factory

    make_session = session_factory()
    with make_session() as session:
        if args.reset:
            removed = reset(session)
            if removed:
                print(f"reset: cleared demo data from {removed} tables")
        _refuse_if_unsafe(session)
        result = build(session)
        session.flush()
        chains = seed_approval_chains(session, result.institution_id)
        version_id, status = solve_and_publish(session, result.period_id)
        session.commit()

    print(f"institution {result.institution_id}")
    print(f"period      {result.period_id}")
    print(f"sessions    {result.sessions}")
    print(f"students    {result.students}")
    print(f"chains      {chains} cadenas de aprobación configuradas")
    print(f"solver      {status}")
    if version_id is None:
        print()
        print("The period was seeded but not solved, so the grid will be empty.")
        return 1
    print(f"published   {version_id}")
    print()
    print("Open the timetable at:")
    print(f"  /timetable?period={result.period_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
