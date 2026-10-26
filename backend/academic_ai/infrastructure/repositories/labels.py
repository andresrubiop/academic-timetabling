"""What a session is *called*, for the people looking at the grid.

Deliberately separate from `snapshot.py`, and deliberately not part of
`SessionSpec`. The scheduling model is about what may be placed where; a
course code is a label. Putting one on `SessionSpec` would mean the optimizer
carried display strings through every LNS iteration, and that a change to how
a section is named touched the type CP-SAT is built from.

So this is a second, narrow read for exactly the fields a screen needs. The
two loads share nothing but the period id, which is the point: an instance is
identical whether or not anybody ever renders it.

The gap this closes: the timetable grid labelled every class with its session
UUID from SESSION 10 to SESSION 22. `metaFor()` in the frontend fabricated a
`SessionMeta` from the id because there was nothing to fabricate it from. A
coordinator cannot use a timetable that names classes by UUID, which made it a
functional gap wearing the costume of a cosmetic one.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

__all__ = [
    "ReferenceLabel",
    "ReferenceLabels",
    "SessionLabel",
    "load_reference_labels",
    "load_session_labels",
]

log = logging.getLogger("academic_ai.repositories.labels")


@dataclass(frozen=True, slots=True)
class SessionLabel:
    """Everything a grid needs to name, group and search one session."""

    session_id: str
    course_code: str
    course_name: str
    section_code: str
    teacher_ids: tuple[str, ...] = ()
    teacher_names: tuple[str, ...] = ()
    #: The programmes this session's offering serves, and their faculties.
    #:
    #: Also carried on `SessionSpec`, and read here rather than joined from
    #: there because the two loads are independent by design -- an instance is
    #: identical whether or not anybody renders it, and a display read that
    #: needed the optimizer's snapshot would couple them. The duplicated join
    #: is one query; the coupling would be permanent.
    program_ids: tuple[str, ...] = ()
    faculty_ids: tuple[str, ...] = ()
    #: Students enrolled in this session, for the STUDENT view. Ids only: a
    #: name here would put personal data into every timetable response.
    student_ids: tuple[str, ...] = field(default=())
    duration_slots: int = 1


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def load_session_labels(
    session: Session, academic_period_id: uuid.UUID | str
) -> Mapping[str, SessionLabel]:
    """Display metadata for every class session in a period.

    Returns an empty mapping rather than raising when the period has no
    sessions. A missing label is a worse timetable; a raised exception here
    would be a *missing* timetable, and the placement and domains this
    accompanies are perfectly valid without it.
    """
    from academic_ai.domain.enums import EnrollmentStatus, SectionStatus
    from academic_ai.infrastructure.db.models import (
        ClassSession,
        Course,
        CourseOffering,
        CourseOfferingProgram,
        Enrollment,
        Program,
        Section,
        SessionTeacher,
        Teacher,
    )

    period_id = _as_uuid(academic_period_id)

    rows = session.execute(
        select(ClassSession, Section, Course)
        .join(Section, Section.id == ClassSession.section_id)
        .join(CourseOffering, CourseOffering.id == Section.course_offering_id)
        .join(Course, Course.id == CourseOffering.course_id)
        .where(ClassSession.academic_period_id == period_id)
        .where(Section.status != SectionStatus.CANCELLED)
    ).all()
    if not rows:
        return {}

    session_ids = [cs.id for cs, _, _ in rows]
    offering_ids = {sec.course_offering_id for _, sec, _ in rows}

    # --- teachers ----------------------------------------------------------
    # The name comes from `teacher.full_name`, not from `app_user`.
    # `teacher.user_id` is nullable -- a lecturer on the timetable need not
    # have a login -- so joining through `app_user` for the name silently drops
    # exactly those teachers, and their classes appear unstaffed on a grid
    # where the staffing is fine.
    teachers: dict[uuid.UUID, list[tuple[str, str]]] = defaultdict(list)
    for cs_id, teacher_id, full_name in session.execute(
        select(SessionTeacher.class_session_id, Teacher.id, Teacher.full_name)
        .join(Teacher, Teacher.id == SessionTeacher.teacher_id)
        .where(SessionTeacher.class_session_id.in_(session_ids))
        .order_by(Teacher.full_name)
    ):
        teachers[cs_id].append((str(teacher_id), full_name or ""))

    # --- programmes and faculties -----------------------------------------
    programs: dict[uuid.UUID, list[str]] = defaultdict(list)
    faculties: dict[uuid.UUID, list[str]] = defaultdict(list)
    for offering_id, program_id, faculty_id in session.execute(
        select(
            CourseOfferingProgram.course_offering_id,
            Program.id,
            Program.faculty_id,
        )
        .join(Program, Program.id == CourseOfferingProgram.program_id)
        .where(CourseOfferingProgram.course_offering_id.in_(offering_ids))
    ):
        programs[offering_id].append(str(program_id))
        if str(faculty_id) not in faculties[offering_id]:
            faculties[offering_id].append(str(faculty_id))

    # --- students ----------------------------------------------------------
    # By section, since that is what an enrolment names. An enrolment with no
    # section is not attributed to one here: ADR-0009 forbids guessing which
    # section a student is in, and this read is not the place to start.
    students_by_section: dict[uuid.UUID, list[str]] = defaultdict(list)
    section_ids = {sec.id for _, sec, _ in rows}
    for section_id, student_id in session.execute(
        select(Enrollment.section_id, Enrollment.student_id)
        .where(Enrollment.section_id.in_(section_ids))
        .where(Enrollment.academic_period_id == period_id)
        .where(
            Enrollment.status.in_(
                (EnrollmentStatus.ENROLLED, EnrollmentStatus.WAITLISTED)
            )
        )
    ):
        students_by_section[section_id].append(str(student_id))

    labels: dict[str, SessionLabel] = {}
    for class_session, section, course in rows:
        pairs = teachers.get(class_session.id, [])
        labels[str(class_session.id)] = SessionLabel(
            session_id=str(class_session.id),
            course_code=course.code,
            course_name=course.name,
            section_code=section.code,
            teacher_ids=tuple(t for t, _ in pairs),
            teacher_names=tuple(n for _, n in pairs),
            program_ids=tuple(programs.get(section.course_offering_id, ())),
            faculty_ids=tuple(faculties.get(section.course_offering_id, ())),
            student_ids=tuple(students_by_section.get(section.id, ())),
            duration_slots=class_session.duration_slots,
        )

    log.info(
        "session labels loaded",
        extra={"period_id": str(period_id), "sessions": len(labels)},
    )
    return labels


@dataclass(frozen=True, slots=True)
class ReferenceLabel:
    """One thing a filter can be set to, with a name somebody can pick."""

    id: str
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class ReferenceLabels:
    """What the timetable's filters offer.

    Sent as a lookup rather than repeated on every session: a period with 399
    sessions across 12 programmes would otherwise carry each programme's name
    399 times, and the client only ever needs the set.

    The gap this closes: the view selector could filter by programme, faculty
    and room since SESSION 19, and the dropdown listed **UUIDs** — because
    nothing sent it names. Only the teacher view worked, because
    `SessionLabel` happened to carry teacher names for the grid. An id in a
    dropdown is not a choice anybody can make.
    """

    programs: tuple[ReferenceLabel, ...] = ()
    faculties: tuple[ReferenceLabel, ...] = ()
    rooms: tuple[ReferenceLabel, ...] = ()


def load_reference_labels(
    session: Session, academic_period_id: uuid.UUID | str
) -> ReferenceLabels:
    """The programmes, faculties and rooms this period actually uses.

    Scoped to the period rather than the institution: a filter offering every
    room the university owns, when this period uses twenty-seven of them, is a
    list somebody has to search rather than choose from.
    """
    from academic_ai.infrastructure.db.models import (
        Campus,
        CourseOffering,
        CourseOfferingProgram,
        Faculty,
        Program,
        Room,
    )

    period_id = _as_uuid(academic_period_id)

    offerings = select(CourseOffering.id).where(
        CourseOffering.academic_period_id == period_id
    )

    programs = session.execute(
        select(Program.id, Program.code, Program.name)
        .join(
            CourseOfferingProgram,
            CourseOfferingProgram.program_id == Program.id,
        )
        .where(CourseOfferingProgram.course_offering_id.in_(offerings))
        .distinct()
        .order_by(Program.name)
    ).all()

    faculties = session.execute(
        select(Faculty.id, Faculty.code, Faculty.name)
        .join(Program, Program.faculty_id == Faculty.id)
        .join(
            CourseOfferingProgram,
            CourseOfferingProgram.program_id == Program.id,
        )
        .where(CourseOfferingProgram.course_offering_id.in_(offerings))
        .distinct()
        .order_by(Faculty.name)
    ).all()

    campuses = select(CourseOffering.campus_id).where(
        CourseOffering.academic_period_id == period_id
    )
    rooms = session.execute(
        select(Room.id, Room.code, Room.name)
        .join(Campus, Campus.id == Room.campus_id)
        .where(Room.campus_id.in_(campuses))
        .order_by(Room.code)
    ).all()

    def as_labels(rows) -> tuple[ReferenceLabel, ...]:
        return tuple(
            ReferenceLabel(id=str(i), code=code or "", name=name or code or str(i))
            for i, code, name in rows
        )

    return ReferenceLabels(
        programs=as_labels(programs),
        faculties=as_labels(faculties),
        rooms=as_labels(rooms),
    )
