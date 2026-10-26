"""Turn a FET file into a real period in the database.

``python -m academic_ai.import_fet Ings-PQBA26.fet``

`infrastructure/fet/reader.py` has read FET files since the FET work, but it
produces a `BaseSnapshot` — an in-memory instance the optimizer can solve. It
writes nothing. `demo.py` writes database rows, but invents them. There was no
bridge, so a real timetable could be solved and never stored, browsed or
changed.

**Teacher names are pseudonymised by default.** A FET file identifies teachers
by their full names, and this one carries ninety-one real people. The
deployment they would land in answers over plain HTTP with no TLS, and
`GET /schedule` has no row-level scope (`SECURITY.md` §9) — so importing them
as they are publishes a named staff timetable to anybody who can reach the
port. `--real-names` turns it off, deliberately as a flag rather than a
default: the safe direction is the one you get without thinking, and the
other one is a decision somebody made.

The mapping is honest about what FET does not have. It has no faculties, no
departments, no curricula and no per-student enrolments; those are synthesised
so the schema's foreign keys have something true to point at, and each
invention is reported.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sys
import uuid
from dataclasses import dataclass

from sqlalchemy import select

#: Everything this command creates is prefixed, like the demo, so nobody has
#: to guess later where a row came from.
FET_PREFIX = "FET"

#: Used only where the file gives no evidence at all — a class it never
#: placed in a room. Small enough not to exclude anything real.
DEFAULT_SEATS = 1

#: FET's "unlimited capacity" sentinel. Kept rather than rewritten -- the
#: reader already reports it as a diagnostic, and silently inventing a number
#: would make a room look measured when it was guessed.
UNLIMITED_CAPACITY = 30000


@dataclass(frozen=True, slots=True)
class ImportResult:
    institution_id: uuid.UUID
    period_id: uuid.UUID
    programs: int
    courses: int
    sessions: int
    teachers: int
    students: int
    rooms: int
    schedule_version_id: uuid.UUID | None
    anonymised: bool
    #: Classes the file placed into no room, though the session needs one.
    roomless_assignments: int = 0
    #: Hours the file declares somebody or something cannot be used.
    blocked_hours: int = 0
    #: Sessions the file restricts to particular rooms, and the rows written.
    restricted_sessions: int = 0
    room_restrictions: int = 0


def program_code_of(group_label: str) -> str:
    """The programme a FET group belongs to, from its name.

    A heuristic over naming conventions, and reported as one. FET carries no
    programme structure the reader preserves, but this institution's groups are
    named `1ro-ITI`, `6to-PsC-A`, and activities shared between groups are
    joined with `+` — sometimes with *student* names rather than group names
    (`ITI+Isaac Castro+Mateo Miño`), which is why the first token is the only
    part worth trusting.

    Returns an empty string when nothing usable is left, and the caller puts
    those sessions in a catch-all programme rather than inventing one each.
    """
    first = group_label.split("+", 1)[0].strip()
    # Drop a level prefix: `1ro-ITI` -> `ITI`, `6to-PsC-A` -> `PsC-A`.
    #
    # The optional letter matters: this file also carries `2doA-NI` and
    # `2doB-NI`, two parallel groups of the *same* second-year programme.
    # Without it they survived as two distinct "programmes" called `2doA-NI`
    # and `2doB-NI`, which is a level and a group masquerading as a degree.
    first = re.sub(r"^\d+(ro|do|to|mo|vo|no)[A-Za-z]?-", "", first)
    # Drop a parallel-group suffix: `PsC-A` -> `PsC`.
    first = re.sub(r"-[A-Z]$", "", first)
    return first.strip()



#: Given names and surnames for pseudonyms. A generated name reads as a person
#: — which is what a timetable is full of — where `Docente 07` reads as a row
#: id and makes every screen look like a database dump.
_GIVEN = (
    "María", "José", "Ana", "Luis", "Carmen", "Jorge", "Elena", "Diego",
    "Lucía", "Andrés", "Paula", "Miguel", "Sofía", "Javier", "Daniela",
    "Fernando", "Valeria", "Ricardo", "Camila", "Sebastián", "Patricia",
    "Álvaro", "Gabriela", "Tomás", "Isabel", "Rodrigo", "Natalia", "Esteban",
)
_SURNAME = (
    "Almeida", "Benítez", "Cabrera", "Delgado", "Espinoza", "Fajardo",
    "Guerrero", "Herrera", "Ibarra", "Jaramillo", "Lozano", "Mendoza",
    "Navarro", "Ochoa", "Paredes", "Quintana", "Ramírez", "Salazar",
    "Terán", "Ulloa", "Vallejo", "Zambrano", "Andrade", "Bustos",
    "Carrión", "Dávila", "Enríquez", "Freire",
)


def seats_per_section(snap) -> dict[str, int]:
    """How many seats each section needs, from where the file put its classes.

    FET records no seat demand, and inventing one is not neutral: too high and
    it excludes the room the class is actually in, too low and it lets the
    optimizer move a cohort into a cupboard. The rooms the file chose are the
    only evidence it contains.

    **The smallest of them**, not the first. A section's sessions sit in
    different rooms across the week, and the section has to fit in *every* one
    of them — taking the first gave a section of 40 sitting in a room of 27,
    and `ROOM_CAPACITY` then excluded the room the file had put it in.

    FET's unlimited-capacity sentinel says nothing about the class, so it is
    ignored rather than declaring a section of thirty thousand.
    """
    capacity_of = {r.id: r.capacity for r in snap.rooms}
    section_of = {s.id: s.section_id for s in snap.sessions}

    smallest: dict[str, int] = {}
    for placed in snap.base_assignments:
        if not placed.room_id:
            continue
        section = section_of.get(placed.session_id)
        capacity = capacity_of.get(placed.room_id)
        if section is None or not capacity or capacity >= UNLIMITED_CAPACITY:
            continue
        smallest[section] = min(smallest.get(section, capacity), capacity)
    return smallest


def pseudonym(index: int) -> str:
    """A stable invented name for the Nth teacher.

    Deterministic in the index rather than random, so re-importing the same
    file twice gives the same people — a timetable whose staff are renamed on
    every import is one nobody can compare with yesterday's.
    """
    #: Positional, like digits in base 28, rather than multiplied.
    #: `(index * 7) % 28` looked varied and was not: gcd(7, 28) is 7, so that
    #: surname only ever took four values, and the whole name repeated every
    #: 28 teachers. With ninety-one of them that put two different people
    #: under one name on the same timetable, which is precisely the thing a
    #: timetable must not do. This is injective up to 28^3.
    given = _GIVEN[index % len(_GIVEN)]
    first = _SURNAME[(index // len(_GIVEN)) % len(_SURNAME)]
    second = _SURNAME[(index // (len(_GIVEN) * len(_SURNAME))) % len(_SURNAME)]
    return f"{given} {first} {second}"


def clean_group_label(label: str) -> str:
    """A group label with the people stripped out of it.

    FET joins the groups sharing an activity with `+`, and this file's labels
    sometimes list **students by name**: `8vo-ITI+Carlos Ulcuango`. Those
    reached the section code, so a timetable rendered somebody's name into
    every block of a class they attend — on a deployment that answers over
    plain HTTP.

    Keeping only the first segment drops them. It also drops genuine second
    groups, which is a real loss: `1ro-NI+1ro-IE` becomes `1ro-NI`. That trade
    is deliberate — a label cannot be both complete and free of names when the
    source mixes the two, and a name is the half that must not survive.
    """
    return label.split("+", 1)[0].strip() or "A"


def split_course_name(raw: str) -> tuple[str, str]:
    """FET's subject string, as a code and a title.

    `PsC_6_Psiquiatría y Psicofarmacología` is a programme, a level and a
    name welded together. Rendered whole it filled every block with
    `PsC_6_Psiq…`; split, the grid shows `PsC-6` over `Psiquiatría y
    Psicofarmacología`, which is what a timetable is supposed to say.

    Anything that does not match the convention is returned as its own title
    with no code, rather than being forced into a shape it does not have.
    """
    parts = raw.split("_", 2)
    if len(parts) == 3 and parts[1].isdigit():
        return f"{parts[0]}-{parts[1]}", parts[2].strip()
    return "", raw.strip()


def refuse_if_unsafe(session, code: str) -> None:
    from academic_ai.infrastructure.db.models import Institution

    rows = session.scalars(select(Institution)).all()
    mine = [i for i in rows if i.code == f"{FET_PREFIX}-{code}"]
    if mine:
        raise SystemExit(
            f"import_fet: {FET_PREFIX}-{code} already exists. Use --reset to "
            "replace it; this command does not merge into an existing period."
        )


def build(
    session,
    path: str,
    *,
    code: str,
    real_names: bool = False,
) -> tuple[ImportResult, list]:
    """Read the file and persist it. Returns the result and the reader's
    diagnostics, which are the honest part of the answer."""
    from academic_ai.domain.enums import (
        AcademicPeriodStatus,
        CourseStatus,
        EnrollmentKind,
        EnrollmentStatus,
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
        Schedule,
        ScheduleAssignment,
        ScheduleVersion,
        Section,
        SessionTeacher,
        Student,
        Teacher,
        Timeslot,
    )
    from academic_ai.infrastructure.fet.reader import read_fet_file

    imported = read_fet_file(path, period_id=code)
    snap = imported.snapshot

    institution = Institution(
        code=f"{FET_PREFIX}-{code}", name=f"Institución {code} (importada de FET)"
    )
    session.add(institution)
    session.flush()

    # FET has no faculty or department. One of each, named as invented.
    faculty = Faculty(
        institution_id=institution.id,
        code=f"{FET_PREFIX}-{code}-FAC",
        name="Facultad (sintética: FET no la registra)",
    )
    session.add(faculty)
    session.flush()
    department = Department(
        faculty_id=faculty.id,
        code=f"{FET_PREFIX}-{code}-DEP",
        name="Departamento (sintético: FET no lo registra)",
    )
    session.add(department)
    session.flush()

    campus = Campus(
        institution_id=institution.id, code=f"{FET_PREFIX}-{code}-CAM", name="Campus"
    )
    session.add(campus)
    session.flush()
    building = Building(
        campus_id=campus.id, code=f"{FET_PREFIX}-{code}-EDI", name="Edificio"
    )
    session.add(building)
    session.flush()

    rooms: dict[str, Room] = {}
    for spec in snap.rooms:
        room = Room(
            building_id=building.id,
            campus_id=campus.id,
            code=spec.id[:32],
            name=spec.id,
            room_type=RoomType.CLASSROOM,
            capacity=spec.capacity or UNLIMITED_CAPACITY,
            status=LifecycleStatus.ACTIVE if spec.is_active else LifecycleStatus.RETIRED,
        )
        session.add(room)
        rooms[spec.id] = room
    session.flush()

    period = AcademicPeriod(
        institution_id=institution.id,
        code=f"{FET_PREFIX}-{code}",
        name=f"Periodo {code}",
        start_date=dt.date(2026, 3, 2),
        end_date=dt.date(2026, 7, 24),
        status=AcademicPeriodStatus.PLANNING,
    )
    session.add(period)
    session.flush()

    slots: dict[str, Timeslot] = {}
    for spec in snap.slots:
        slot = Timeslot(
            academic_period_id=period.id,
            day_of_week=spec.day_of_week,
            start_minute=spec.start_minute,
            end_minute=spec.start_minute + 60,
            day_slot_index=spec.in_day_index,
            global_slot_index=spec.global_index,
            is_schedulable=spec.is_schedulable,
            label=f"{spec.start_minute // 60:02d}:{spec.start_minute % 60:02d}",
        )
        session.add(slot)
        slots[spec.id] = slot
    session.flush()

    teachers: dict[str, Teacher] = {}
    for index, spec in enumerate(sorted(snap.teachers, key=lambda t: t.id), 1):
        teacher = Teacher(
            institution_id=institution.id,
            department_id=department.id,
            code=f"{FET_PREFIX}-D{index:03d}",
            # The one decision with a person on the other end of it.
            full_name=spec.id if real_names else pseudonym(index),
            contract_type=TeacherContractType.FULL_TIME,
        )
        session.add(teacher)
        teachers[spec.id] = teacher
    session.flush()

    # --- programmes, derived from group names -----------------------------
    def program_for(section_label: str) -> str:
        return program_code_of(section_label.rsplit("|", 1)[-1]) or "SIN-PROGRAMA"

    program_codes = sorted({program_for(s.section_id) for s in snap.sessions})
    programs: dict[str, Program] = {}
    curricula: dict[str, tuple[CurriculumVersion, CurriculumLevel]] = {}
    for pcode in program_codes:
        program = Program(
            faculty_id=faculty.id,
            department_id=department.id,
            code=f"{FET_PREFIX}-{pcode}"[:32],
            name=pcode,
            degree_title=pcode,
        )
        session.add(program)
        session.flush()
        programs[pcode] = program

        # FET has no curricula, and `enrollment` cannot exist without one.
        curriculum = Curriculum(
            program_id=program.id,
            code=f"{FET_PREFIX}-{pcode}-MALLA"[:32],
            name=f"Malla {pcode} (sintética)",
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
            curriculum_version_id=version.id, level_number=1, name="Nivel único"
        )
        session.add(level)
        session.flush()
        curricula[pcode] = (version, level)

    # --- courses, offerings, sections, sessions ---------------------------
    courses: dict[str, Course] = {}
    used_codes: set[str] = set()
    for index, course_key in enumerate(
        sorted({s.course_id for s in snap.sessions}), 1
    ):
        derived, title = split_course_name(course_key)
        # `FET-C0120` told a reader nothing. `PsC-6` is the programme and the
        # level, which is what the block has room to show.
        code = derived or f"C{index:04d}"
        candidate, suffix = code, 2
        while candidate in used_codes:
            candidate = f"{code}.{suffix}"
            suffix += 1
        used_codes.add(candidate)

        course = Course(
            institution_id=institution.id,
            department_id=department.id,
            code=candidate[:32],
            name=title[:200] or course_key[:200],
            credits=4,
            theory_hours=4,
            status=CourseStatus.ACTIVE,
        )
        session.add(course)
        courses[course_key] = course
    session.flush()

    # **One offering per course**, not per FET "offering".
    #
    # ADR-0004 makes `CourseOffering` unique on (course, period, campus,
    # modality, variant): it is "that course opened in this period". FET's 267
    # offering ids are `subject|group` — the same subject taught to several
    # groups — which in this model is one offering with several *sections*.
    # The database refused the naive mapping, correctly, with a unique
    # violation.
    #
    # The mapping is better for it: a course several groups take now produces
    # one offering serving several programmes, which is exactly the shared
    # -course case the scope rules and approval routing exist for.
    section_seats = seats_per_section(snap)

    offerings: dict[str, CourseOffering] = {}
    sections: dict[str, Section] = {}
    section_programs: dict[str, str] = {}
    served: set[tuple[str, str]] = set()
    curriculum_courses: dict[tuple[str, str], CurriculumCourse] = {}
    section_codes: dict[str, set[str]] = {}

    for spec in snap.sessions:
        pcode = program_for(spec.section_id)

        if spec.course_id not in offerings:
            offering = CourseOffering(
                course_id=courses[spec.course_id].id,
                academic_period_id=period.id,
                campus_id=campus.id,
                modality=Modality.ONSITE,
                status=OfferingStatus.OPEN,
            )
            session.add(offering)
            session.flush()
            offerings[spec.course_id] = offering
        offering = offerings[spec.course_id]

        if (spec.course_id, pcode) not in served:
            served.add((spec.course_id, pcode))
            session.add(
                CourseOfferingProgram(
                    course_offering_id=offering.id,
                    program_id=programs[pcode].id,
                    # The first programme to claim a course is its core; the
                    # rest take it as a service. FET does not say which, and
                    # guessing differently would be no more true.
                    service_role=(
                        ServiceRole.CORE
                        if sum(1 for c, _ in served if c == spec.course_id) == 1
                        else ServiceRole.SERVICE
                    ),
                )
            )
            key = (pcode, spec.course_id)
            if key not in curriculum_courses:
                version, level = curricula[pcode]
                cc = CurriculumCourse(
                    curriculum_version_id=version.id,
                    curriculum_level_id=level.id,
                    course_id=courses[spec.course_id].id,
                    requirement_type=RequirementType.MANDATORY,
                )
                session.add(cc)
                session.flush()
                curriculum_courses[key] = cc
            session.flush()

        if spec.section_id not in sections:
            # Capacity from the room the file itself assigned, not from a
            # number invented here.
            #
            # FET records no seat demand, and defaulting to 30 was a fabricated
            # figure that then *contradicted the file*: 40 sessions ended up
            # with no candidate room at all, because a class the file placed in
            # a 25-seat room was declared to need 30 seats and `ROOM_CAPACITY`
            # excluded the very room it was in.
            #
            # The file's own placement is the best evidence available: it put
            # this class there, so it fits there.
            seats = section_seats.get(
                spec.section_id, max(1, spec.seat_demand or DEFAULT_SEATS)
            )
            # The group's own label, so a section is recognisable as the group
            # it serves rather than as "A", "B", "C".
            label = clean_group_label(spec.section_id.rsplit("|", 1)[-1])[:32]
            taken = section_codes.setdefault(spec.course_id, set())
            code_candidate = label
            suffix = 2
            while code_candidate in taken:
                code_candidate = f"{label[:28]}-{suffix}"
                suffix += 1
            taken.add(code_candidate)

            section = Section(
                course_offering_id=offering.id,
                academic_period_id=period.id,
                code=code_candidate,
                capacity=seats,
                status=SectionStatus.OPEN,
            )
            session.add(section)
            session.flush()
            sections[spec.section_id] = section
            section_programs[spec.section_id] = pcode

    # Which activities the file actually placed in a room.
    #
    # `schedule_assignment` carries a **composite** foreign key
    # `(class_session_id, requires_room)` back to `class_session`, so an
    # assignment cannot claim a different room requirement than its session —
    # the schema makes them impossible to disagree. For an imported timetable
    # that settles the question the right way round: whether a session needs a
    # room is evidenced by whether the file gave it one.
    placed_in_a_room = {
        a.session_id for a in snap.base_assignments if a.room_id
    }
    roomless_sessions = 0

    class_sessions: dict[str, ClassSession] = {}
    per_section: dict[str, int] = {}
    for spec in snap.sessions:
        per_section[spec.section_id] = per_section.get(spec.section_id, 0) + 1
        class_session = ClassSession(
            section_id=sections[spec.section_id].id,
            academic_period_id=period.id,
            sequence_number=per_section[spec.section_id],
            session_type=SessionType.LECTURE,
            delivery_mode=Modality.ONSITE,
            duration_slots=spec.duration_slots,
            requires_timeslot=spec.requires_timeslot,
            requires_room=(
                spec.requires_room
                if not snap.base_assignments
                else spec.id in placed_in_a_room
            ),
        )
        if spec.requires_room and spec.id not in placed_in_a_room:
            roomless_sessions += 1
        session.add(class_session)
        session.flush()
        class_sessions[spec.id] = class_session
        for teacher_key in spec.teacher_ids:
            if teacher_key in teachers:
                session.add(
                    SessionTeacher(
                        class_session_id=class_session.id,
                        teacher_id=teachers[teacher_key].id,
                        role=TeacherRole.PRIMARY,
                    )
                )
    session.flush()

    # --- students -----------------------------------------------------------
    # FET has student *sets*, not students. The reader synthesises one
    # indivisible student per set and says so; that is carried through here
    # rather than quietly turned into a roll.
    students: dict[str, Student] = {}
    for index, student_key in enumerate(
        sorted({e.student_id for e in snap.enrollments}), 1
    ):
        pcode = program_code_of(student_key) or "SIN-PROGRAMA"
        program = programs.get(pcode) or programs[program_codes[0]]
        student = Student(
            institution_id=institution.id,
            program_id=program.id,
            code=f"{FET_PREFIX}-G{index:04d}",
            full_name=f"Grupo {student_key}",
        )
        session.add(student)
        students[student_key] = student
    session.flush()

    course_of_fet_offering = {
        s.offering_id: s.course_id for s in snap.sessions
    }
    program_of_fet_offering = {
        s.offering_id: program_for(s.section_id) for s in snap.sessions
    }
    for spec in snap.enrollments:
        course_key = course_of_fet_offering.get(spec.offering_id)
        offering = offerings.get(course_key)
        pcode = program_of_fet_offering.get(spec.offering_id)
        if offering is None or pcode is None:
            continue
        version, _level = curricula[pcode]
        cc = curriculum_courses.get((pcode, course_key))
        if cc is None:
            continue
        session.add(
            Enrollment(
                student_id=students[spec.student_id].id,
                course_offering_id=offering.id,
                section_id=None,
                course_id=courses[course_key].id,
                academic_period_id=period.id,
                curriculum_version_id=version.id,
                curriculum_course_id=cc.id,
                is_mandatory=spec.is_mandatory,
                enrollment_kind=EnrollmentKind.CURRENT_LEVEL,
                attempt_number=spec.attempt_number,
                status=EnrollmentStatus.ENROLLED,
            )
        )
    session.flush()

    # --- the timetable the file already carries ---------------------------
    # --- allowed rooms ------------------------------------------------------
    # `ConstraintSubjectPreferredRooms` and `ConstraintActivityPreferredRoom(s)`
    # — 226 of them in this file, restricting 374 of the 399 sessions. The
    # reader has resolved them down to sessions since the FET work and this
    # importer dropped every one, so `compute_domains` offered every room for
    # every class: the grid proposed drops into rooms the file forbids, and a
    # re-optimisation would scatter laboratories into lecture halls.
    from academic_ai.infrastructure.db.models import ClassSessionAllowedRoom

    room_restrictions = 0
    restricted_sessions = 0
    for spec in snap.sessions:
        class_session = class_sessions.get(spec.id)
        if class_session is None or not spec.allowed_room_ids:
            continue
        written = 0
        for room_key in spec.allowed_room_ids:
            room = rooms.get(room_key)
            if room is None:
                continue
            session.add(
                ClassSessionAllowedRoom(
                    class_session_id=class_session.id, room_id=room.id
                )
            )
            written += 1
        if written:
            restricted_sessions += 1
            room_restrictions += written
    session.flush()

    # --- availability -------------------------------------------------------
    # The file's own time restrictions. `ConstraintTeacherNotAvailableTimes`
    # and `ConstraintRoomNotAvailableTimes` are read by the FET reader into
    # `unavailable_slot_ids`, and this importer dropped them on the floor: 42
    # of the 91 teachers in this file declare unavailability, 1 121 blocked
    # hours in total, and none of them reached the database. The timetable
    # imported was therefore one the optimizer would consider re-planning
    # without any of the constraints its author had entered.
    from academic_ai.domain.enums import AvailabilityStatus
    from academic_ai.infrastructure.db.models import Availability

    blocked_hours = 0
    for spec in snap.teachers:
        teacher = teachers.get(spec.id)
        if teacher is None:
            continue
        for slot_key in spec.unavailable_slot_ids:
            slot = slots.get(slot_key)
            if slot is None:
                continue
            session.add(
                Availability(
                    teacher_id=teacher.id,
                    timeslot_id=slot.id,
                    status=AvailabilityStatus.UNAVAILABLE,
                    reason="Importado de FET",
                )
            )
            blocked_hours += 1

    for spec in snap.rooms:
        room = rooms.get(spec.id)
        if room is None:
            continue
        for slot_key in spec.unavailable_slot_ids:
            slot = slots.get(slot_key)
            if slot is None:
                continue
            session.add(
                Availability(
                    room_id=room.id,
                    timeslot_id=slot.id,
                    status=AvailabilityStatus.UNAVAILABLE,
                    reason="Importado de FET",
                )
            )
            blocked_hours += 1
    session.flush()

    # A seeded institution needs configured approval chains, or a change can
    # be proposed and never submitted: the router refuses when no workflow
    # exists, deliberately (SESSION 12). Configuration, not a fallback.
    from academic_ai.demo import seed_approval_chains

    seed_approval_chains(session, institution.id, prefix=FET_PREFIX)

    version_id = None
    roomless = roomless_sessions
    if snap.base_assignments:
        from academic_ai.demo import _an_administrator

        schedule = Schedule(
            institution_id=institution.id,
            academic_period_id=period.id,
            scope=ScheduleScope.INSTITUTION,
            code=f"{FET_PREFIX}-{code}-MAIN"[:32],
            name=f"Horario importado de FET ({code})",
        )
        session.add(schedule)
        session.flush()
        # DRAFT, then published: assignments cannot be added to a version that
        # is already PUBLISHED.
        schedule_version = ScheduleVersion(
            schedule_id=schedule.id,
            institution_id=institution.id,
            academic_period_id=period.id,
            version_number=1,
            status=ScheduleVersionStatus.DRAFT,
        )
        session.add(schedule_version)
        session.flush()

        for assignment in snap.base_assignments:
            class_session = class_sessions.get(assignment.session_id)
            slot = slots.get(assignment.start_slot_id)
            if class_session is None or slot is None:
                continue
            room = rooms.get(assignment.room_id) if assignment.room_id else None
            session.add(
                ScheduleAssignment(
                    schedule_version_id=schedule_version.id,
                    class_session_id=class_session.id,
                    academic_period_id=period.id,
                    start_timeslot_id=slot.id,
                    day_of_week=slot.day_of_week,
                    start_slot_index=slot.day_slot_index,
                    duration_slots=class_session.duration_slots,
                    requires_room=room is not None,
                    room_id=room.id if room is not None else None,
                )
            )
        session.flush()
        # Resolved *before* the status changes. `_an_administrator` runs a
        # query, which triggers autoflush — and autoflush of a version already
        # marked PUBLISHED with nobody's name on it hits
        # `ck_schedule_version_published_requires_publisher` before the name
        # can be set.
        publisher = _an_administrator(session)
        schedule_version.status = ScheduleVersionStatus.PUBLISHED
        schedule_version.published_at = dt.datetime.now(dt.timezone.utc)
        schedule_version.published_by_user_id = publisher
        session.flush()
        version_id = schedule_version.id

    return (
        ImportResult(
            institution_id=institution.id,
            period_id=period.id,
            programs=len(programs),
            courses=len(courses),
            sessions=len(class_sessions),
            teachers=len(teachers),
            students=len(students),
            rooms=len(rooms),
            schedule_version_id=version_id,
            anonymised=not real_names,
            roomless_assignments=roomless,
            blocked_hours=blocked_hours,
            restricted_sessions=restricted_sessions,
            room_restrictions=room_restrictions,
        ),
        list(imported.diagnostics),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m academic_ai.import_fet")
    parser.add_argument("path", help="the .fet file")
    parser.add_argument("--code", default="FET26", help="a short code for the period")
    parser.add_argument(
        "--real-names",
        action="store_true",
        help="Import teacher names as they appear in the file. Off by default: "
        "a FET file identifies teachers by their real names, and this "
        "deployment answers over plain HTTP with no row-level scope on the "
        "timetable.",
    )
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    from academic_ai.infrastructure.db.session import session_factory

    make_session = session_factory()
    with make_session() as session:
        if args.reset:
            from academic_ai.demo import reset

            # Scoped to what *this* command created, not to the demo's rows.
            reset(session, prefix=FET_PREFIX)
        refuse_if_unsafe(session, args.code)
        result, diagnostics = build(
            session, args.path, code=args.code, real_names=args.real_names
        )
        session.commit()

    print(f"institution {result.institution_id}")
    print(f"period      {result.period_id}")
    print(
        f"imported    {result.sessions} sesiones, {result.courses} cursos, "
        f"{result.programs} programas, {result.teachers} docentes, "
        f"{result.rooms} aulas, {result.students} grupos"
    )
    print(f"names       {'PSEUDONYMISED' if result.anonymised else 'REAL'}")
    if result.schedule_version_id:
        print(f"published   {result.schedule_version_id}")
    if result.restricted_sessions:
        print(
            f"rooms       {result.restricted_sessions} sesiones restringidas a "
            f"aulas concretas ({result.room_restrictions} combinaciones)"
        )
    if result.blocked_hours:
        print(
            f"blocked     {result.blocked_hours} horas no disponibles "
            "importadas del archivo"
        )
    if result.roomless_assignments:
        print(
            f"roomless    {result.roomless_assignments} clases que pedían aula "
            "aparecen en el archivo sin ninguna"
        )
    print()
    print("What FET does not carry, and what was invented for it:")
    for diagnostic in diagnostics:
        print(f"  [{diagnostic.code.value}] {diagnostic.message[:100]}")
    print()
    print(f"  /timetable?period={result.period_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
