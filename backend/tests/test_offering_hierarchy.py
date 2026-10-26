"""Course > CourseOffering > Section > ClassSession, and session-level teaching."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from academic_ai.domain.enums import (
    AcademicPeriodStatus,
    Modality,
    OfferingStatus,
    SectionStatus,
    SessionType,
    TeacherContractType,
    TeacherRole,
)
from academic_ai.infrastructure.db.models import (
    AcademicPeriod,
    ClassSession,
    CourseOffering,
    Section,
    SessionTeacher,
    Teacher,
)
from academic_ai.infrastructure.db.models.offering import (
    TIME_OCCUPYING_TEACHER_ROLES,
)
from tests.conftest import AcademicWorld


class TestOfferingIdentity:
    def test_an_offering_names_exactly_one_course(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """Merging never changes an offering's course.

        A student's enrollment must always name the course their curriculum
        required; an offering that pointed at several courses would make
        repeat-attempt counting and alternative-offering counting
        unanswerable.
        """
        assert offered.offering.course_id == offered.course.id

    def test_duplicate_offerings_are_rejected(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """`variant_code` is NULL here, so this exercises NULLS NOT DISTINCT."""
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    CourseOffering(
                        course_id=offered.course.id,
                        academic_period_id=offered.period.id,
                        campus_id=offered.campus.id,
                        modality=Modality.ONSITE,
                        status=OfferingStatus.OPEN,
                    )
                )
                session.flush()

    def test_a_variant_distinguishes_two_otherwise_identical_offerings(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        session.add(
            CourseOffering(
                course_id=offered.course.id,
                academic_period_id=offered.period.id,
                campus_id=offered.campus.id,
                modality=Modality.ONSITE,
                variant_code="NOCHE",
                status=OfferingStatus.OPEN,
            )
        )
        session.flush()


class TestPeriodBinding:
    def test_a_section_cannot_claim_another_period(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        other_period = AcademicPeriod(
            institution_id=offered.institution.id,
            code="2026-2",
            name="Periodo 2026-2",
            start_date=dt.date(2026, 9, 1),
            end_date=dt.date(2027, 1, 31),
            status=AcademicPeriodStatus.PLANNING,
        )
        session.add(other_period)
        session.flush()

        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    Section(
                        course_offering_id=offered.offering.id,
                        academic_period_id=other_period.id,
                        code="X",
                        capacity=10,
                        status=SectionStatus.OPEN,
                    )
                )
                session.flush()


class TestSchedulableUnit:
    def test_an_asynchronous_session_needs_no_room(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        async_session = ClassSession(
            section_id=offered.section.id,
            academic_period_id=offered.period.id,
            sequence_number=9,
            session_type=SessionType.LECTURE,
            delivery_mode=Modality.ONLINE,
            duration_slots=1,
            requires_timeslot=False,
            requires_room=False,
        )
        session.add(async_session)
        session.flush()
        assert async_session.requires_timeslot is False

    def test_a_session_that_needs_a_room_must_need_a_timeslot(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    ClassSession(
                        section_id=offered.section.id,
                        academic_period_id=offered.period.id,
                        sequence_number=10,
                        session_type=SessionType.LAB,
                        delivery_mode=Modality.ONSITE,
                        duration_slots=1,
                        requires_timeslot=False,
                        requires_room=True,
                    )
                )
                session.flush()

    def test_duration_must_be_positive(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    ClassSession(
                        section_id=offered.section.id,
                        academic_period_id=offered.period.id,
                        sequence_number=11,
                        session_type=SessionType.LECTURE,
                        delivery_mode=Modality.ONSITE,
                        duration_slots=0,
                    )
                )
                session.flush()

    def test_a_section_may_mix_session_types(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """A lecture block plus a lab block — the reason Session is a
        separate entity from Section at all."""
        session.add(
            ClassSession(
                section_id=offered.section.id,
                academic_period_id=offered.period.id,
                sequence_number=2,
                session_type=SessionType.LAB,
                delivery_mode=Modality.ONSITE,
                duration_slots=2,
            )
        )
        session.flush()

        types = {s.session_type for s in offered.section.sessions}
        assert types == {SessionType.LECTURE, SessionType.LAB}


class TestTeacherAssignment:
    def test_teaching_is_recorded_per_session(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        assert len(offered.class_session.teachers) == 1
        assert offered.class_session.teachers[0].teacher_id == offered.teacher.id

    def test_a_lab_may_have_a_different_instructor(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """With session-level assignment this needs no resolution rule: the
        lab simply names its own teacher."""
        lab_teacher = Teacher(
            institution_id=offered.institution.id,
            department_id=offered.department.id,
            code="T002",
            full_name="Katherine Johnson",
            contract_type=TeacherContractType.PART_TIME,
        )
        session.add(lab_teacher)
        session.flush()

        lab = ClassSession(
            section_id=offered.section.id,
            academic_period_id=offered.period.id,
            sequence_number=2,
            session_type=SessionType.LAB,
            delivery_mode=Modality.ONSITE,
            duration_slots=2,
        )
        session.add(lab)
        session.flush()

        session.add(
            SessionTeacher(
                class_session_id=lab.id,
                teacher_id=lab_teacher.id,
                role=TeacherRole.PRIMARY,
            )
        )
        session.flush()

        assert lab.teachers[0].teacher_id == lab_teacher.id
        assert offered.class_session.teachers[0].teacher_id == offered.teacher.id

    def test_co_teaching_is_two_rows(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        second = Teacher(
            institution_id=offered.institution.id,
            department_id=offered.department.id,
            code="T003",
            full_name="Mary Jackson",
            contract_type=TeacherContractType.FULL_TIME,
        )
        session.add(second)
        session.flush()

        session.add(
            SessionTeacher(
                class_session_id=offered.class_session.id,
                teacher_id=second.id,
                role=TeacherRole.ASSISTANT,
            )
        )
        session.flush()

        session.refresh(offered.class_session)
        assert len(offered.class_session.teachers) == 2

    def test_a_teacher_is_assigned_to_a_session_once(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    SessionTeacher(
                        class_session_id=offered.class_session.id,
                        teacher_id=offered.teacher.id,
                        role=TeacherRole.ASSISTANT,
                    )
                )
                session.flush()

    def test_only_time_occupying_roles_enter_the_overlap_constraint(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """A substitute must not silently occupy the teacher's time.

        The rule lives in one named constant rather than a string list inside
        the optimizer.
        """
        assert TeacherRole.PRIMARY in TIME_OCCUPYING_TEACHER_ROLES
        assert TeacherRole.ASSISTANT in TIME_OCCUPYING_TEACHER_ROLES
        assert TeacherRole.SUBSTITUTE not in TIME_OCCUPYING_TEACHER_ROLES

        primary = offered.class_session.teachers[0]
        assert primary.occupies_time is True

        substitute_teacher = Teacher(
            institution_id=offered.institution.id,
            code="T004",
            full_name="Substitute",
            contract_type=TeacherContractType.HOURLY,
        )
        session.add(substitute_teacher)
        session.flush()

        substitute = SessionTeacher(
            class_session_id=offered.class_session.id,
            teacher_id=substitute_teacher.id,
            role=TeacherRole.SUBSTITUTE,
        )
        session.add(substitute)
        session.flush()
        assert substitute.occupies_time is False
