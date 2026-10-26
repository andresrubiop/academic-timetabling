"""SESSION 22: what a session is called.

From SESSION 10 to SESSION 22 the timetable grid labelled every class with its
session UUID, because nothing sent it anything else -- `metaFor()` in the
frontend fabricated a `SessionMeta` from the id. A coordinator cannot use a
timetable that names classes by UUID, which made this a functional gap wearing
the costume of a cosmetic one.

The read is deliberately separate from `load_snapshot`: the scheduling model
carries no display strings, so a course code never travels through an LNS
iteration.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.orm import Session

from academic_ai.domain.enums import (
    EnrollmentKind,
    EnrollmentStatus,
    Modality,
    SectionStatus,
    ServiceRole,
    SessionType,
)
from academic_ai.infrastructure.db.models import (
    ClassSession,
    CourseOffering,
    CourseOfferingProgram,
    Enrollment,
    Section,
    SessionTeacher,
)
from academic_ai.infrastructure.repositories import load_session_labels


class TestLabels:
    def test_a_session_is_named_by_its_course_and_section(
        self, session: Session, offered
    ) -> None:
        labels = load_session_labels(session, offered.period.id)
        label = labels[str(offered.class_session.id)]

        # The whole point: a code a person recognises, not a UUID.
        assert label.course_code == offered.course.code
        assert label.course_name == offered.course.name
        assert label.section_code == offered.section.code
        assert label.session_id != label.course_code

    def test_the_teacher_is_named(self, session: Session, offered) -> None:
        label = load_session_labels(session, offered.period.id)[
            str(offered.class_session.id)
        ]
        assert label.teacher_ids == (str(offered.teacher.id),)
        assert label.teacher_names[0] == offered.teacher.full_name

    def test_a_teacher_without_a_login_is_still_named(
        self, session: Session, offered
    ) -> None:
        # `teacher.user_id` is nullable: a lecturer on the timetable need not
        # have an account. Reading the name through `app_user` would drop
        # exactly those teachers, and their classes would appear unstaffed on
        # a grid where the staffing is fine. The first version of this loader
        # did precisely that.
        offered.teacher.user_id = None
        session.flush()

        label = load_session_labels(session, offered.period.id)[
            str(offered.class_session.id)
        ]
        assert label.teacher_names == (offered.teacher.full_name,)

    def test_duration_comes_from_the_session(
        self, session: Session, offered
    ) -> None:
        label = load_session_labels(session, offered.period.id)[
            str(offered.class_session.id)
        ]
        assert label.duration_slots == offered.class_session.duration_slots

    def test_a_session_with_no_teacher_is_labelled_anyway(
        self, session: Session, offered
    ) -> None:
        # SESSION 6's category: a class nobody is committed to teach is a
        # staffing problem, and it still has to appear on the timetable.
        session.query(SessionTeacher).filter(
            SessionTeacher.class_session_id == offered.class_session.id
        ).delete()
        session.flush()

        label = load_session_labels(session, offered.period.id)[
            str(offered.class_session.id)
        ]
        assert label.course_code == offered.course.code
        assert label.teacher_names == ()

    def test_a_period_with_no_sessions_returns_nothing_rather_than_raising(
        self, session: Session, world
    ) -> None:
        # A missing label is a worse timetable; raising here would be a
        # *missing* timetable, and the placement this accompanies is valid
        # without any labels at all.
        assert load_session_labels(session, world.period.id) == {}

    def test_an_unknown_period_returns_nothing(self, session: Session) -> None:
        assert load_session_labels(session, uuid.uuid4()) == {}

    def test_a_cancelled_section_is_not_labelled(
        self, session: Session, offered
    ) -> None:
        # It is not in the snapshot either, so labelling it would put a class
        # on the grid that the solver never places.
        offered.section.status = SectionStatus.CANCELLED
        session.flush()
        assert load_session_labels(session, offered.period.id) == {}


class TestProgrammeMembership:
    def test_a_shared_course_carries_every_programme_it_serves(
        self, session: Session, offered
    ) -> None:
        # The reason `program_ids` is a list. Filtering by programme has to
        # match *any* of them, or a course shared with a second programme
        # disappears from the one that takes it as a service -- which is the
        # ordinary case for common-core courses, not an exotic one.
        session.add(
            CourseOfferingProgram(
                course_offering_id=offered.offering.id,
                program_id=offered.other_program.id,
                service_role=ServiceRole.SERVICE,
            )
        )
        session.flush()

        label = load_session_labels(session, offered.period.id)[
            str(offered.class_session.id)
        ]
        assert len(label.program_ids) == 2
        assert str(offered.program.id) in label.program_ids
        assert str(offered.other_program.id) in label.program_ids

    def test_the_faculty_comes_from_the_programme(
        self, session: Session, offered
    ) -> None:
        label = load_session_labels(session, offered.period.id)[
            str(offered.class_session.id)
        ]
        assert label.faculty_ids == (str(offered.faculty.id),)


class TestStudents:
    def _enrol(self, session: Session, world, *, section) -> None:
        session.add(
            Enrollment(
                student_id=world.student.id,
                course_offering_id=world.offering.id,
                section_id=section.id if section else None,
                course_id=world.course.id,
                academic_period_id=world.period.id,
                curriculum_version_id=world.curriculum_version.id,
                curriculum_course_id=world.curriculum_course.id,
                is_mandatory=True,
                enrollment_kind=EnrollmentKind.CURRENT_LEVEL,
                attempt_number=1,
                status=EnrollmentStatus.ENROLLED,
            )
        )
        session.flush()

    def test_an_enrolled_student_appears_on_their_section(
        self, session: Session, offered
    ) -> None:
        self._enrol(session, offered, section=offered.section)
        label = load_session_labels(session, offered.period.id)[
            str(offered.class_session.id)
        ]
        assert label.student_ids == (str(offered.student.id),)

    def test_an_enrolment_with_no_section_is_not_attributed_to_one(
        self, session: Session, offered
    ) -> None:
        # ADR-0009 forbids guessing which section a student is in, and a
        # display read is not the place to start. The student would otherwise
        # appear on a class nobody assigned them to.
        self._enrol(session, offered, section=None)
        label = load_session_labels(session, offered.period.id)[
            str(offered.class_session.id)
        ]
        assert label.student_ids == ()

    def test_ids_only_never_names(self, session: Session, offered) -> None:
        # A student name here would put personal data into every timetable
        # response, read by everyone who can see the grid.
        self._enrol(session, offered, section=offered.section)
        label = load_session_labels(session, offered.period.id)[
            str(offered.class_session.id)
        ]
        for value in label.student_ids:
            uuid.UUID(value)  # raises if it is anything but an id
