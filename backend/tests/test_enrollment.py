"""Enrollment: the authoritative, immutable record of academic intent.

Every test here defends a property that cannot be repaired after the fact.
You cannot backfill which curriculum requirement a two-year-old enrollment
satisfied, and you cannot un-corrupt an enrollment that points at an offering
from a different period.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session

from academic_ai.domain.enums import (
    AcademicPeriodStatus,
    EnrollmentKind,
    EnrollmentStatus,
    Modality,
    OfferingStatus,
    SectionStatus,
)
from academic_ai.infrastructure.db.models import (
    AcademicPeriod,
    CourseOffering,
    Enrollment,
    Section,
)
from tests.conftest import AcademicWorld


def _enrollment(world: AcademicWorld, **overrides: object) -> Enrollment:
    """A valid enrollment for the seeded student in the seeded offering."""
    kwargs: dict[str, object] = {
        "student_id": world.student.id,
        "course_offering_id": world.offering.id,
        "course_id": world.course.id,
        "academic_period_id": world.period.id,
        "curriculum_version_id": world.curriculum_version.id,
        "curriculum_course_id": world.curriculum_course.id,
        "is_mandatory": True,
        "enrollment_kind": EnrollmentKind.CURRENT_LEVEL,
        "attempt_number": 1,
        "status": EnrollmentStatus.ENROLLED,
    }
    kwargs.update(overrides)
    return Enrollment(**kwargs)


class TestSnapshotBinding:
    """The denormalised snapshot cannot disagree with the offering."""

    def test_valid_enrollment_is_accepted(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        enrollment = _enrollment(offered)
        session.add(enrollment)
        session.flush()
        assert enrollment.id is not None

    def test_course_must_be_the_offering_s_course(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(_enrollment(offered, course_id=offered.other_course.id))
                session.flush()

    def test_period_must_be_the_offering_s_period(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        other_period = AcademicPeriod(
            institution_id=offered.institution.id,
            code="2026-2",
            name="Periodo 2026-2",
            start_date=dt.date(2026, 9, 1),
            end_date=dt.date(2027, 1, 31),
            slot_minutes=60,
            status=AcademicPeriodStatus.PLANNING,
        )
        session.add(other_period)
        session.flush()

        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    _enrollment(offered, academic_period_id=other_period.id)
                )
                session.flush()

    def test_section_must_belong_to_the_enrollment_s_offering(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        foreign_offering = CourseOffering(
            course_id=offered.other_course.id,
            academic_period_id=offered.period.id,
            campus_id=offered.campus.id,
            modality=Modality.ONSITE,
            status=OfferingStatus.OPEN,
        )
        session.add(foreign_offering)
        session.flush()

        foreign_section = Section(
            course_offering_id=foreign_offering.id,
            academic_period_id=offered.period.id,
            code="A",
            capacity=30,
            status=SectionStatus.OPEN,
        )
        session.add(foreign_section)
        session.flush()

        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(_enrollment(offered, section_id=foreign_section.id))
                session.flush()

    def test_a_student_enrolls_in_an_offering_only_once(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        session.add(_enrollment(offered))
        session.flush()

        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(_enrollment(offered))
                session.flush()


class TestAcademicMeaning:
    def test_mandatory_enrollment_must_name_its_curriculum_requirement(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """Otherwise "mandatory under which plan?" is unanswerable, and
        SESSION 5 progression risk cannot be computed."""
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    _enrollment(offered, is_mandatory=True, curriculum_course_id=None)
                )
                session.flush()

    def test_a_free_elective_needs_no_curriculum_requirement(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        enrollment = _enrollment(
            offered,
            is_mandatory=False,
            curriculum_course_id=None,
            enrollment_kind=EnrollmentKind.ELECTIVE,
        )
        session.add(enrollment)
        session.flush()
        assert enrollment.id is not None

    def test_a_second_attempt_must_be_marked_repeated(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    _enrollment(
                        offered,
                        attempt_number=2,
                        enrollment_kind=EnrollmentKind.CURRENT_LEVEL,
                    )
                )
                session.flush()

    def test_a_repeated_second_attempt_is_accepted(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        enrollment = _enrollment(
            offered, attempt_number=2, enrollment_kind=EnrollmentKind.REPEATED
        )
        session.add(enrollment)
        session.flush()
        assert enrollment.attempt_number == 2

    def test_attempt_number_must_be_positive(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(_enrollment(offered, attempt_number=0))
                session.flush()


class TestSnapshotImmutability:
    """Once confirmed, the academic snapshot is frozen — by trigger."""

    def test_snapshot_cannot_change_after_confirmation(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        enrollment = _enrollment(offered, status=EnrollmentStatus.ENROLLED)
        session.add(enrollment)
        session.flush()

        enrollment.is_mandatory = False
        with pytest.raises(DatabaseError):
            with session.begin_nested():
                session.flush()

    def test_snapshot_may_be_corrected_while_still_requested(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        enrollment = _enrollment(
            offered,
            status=EnrollmentStatus.REQUESTED,
            enrollment_kind=EnrollmentKind.CURRENT_LEVEL,
        )
        session.add(enrollment)
        session.flush()

        enrollment.enrollment_kind = EnrollmentKind.ADVANCED
        session.flush()
        assert enrollment.enrollment_kind is EnrollmentKind.ADVANCED

    def test_status_and_section_remain_editable_after_confirmation(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """Sectioning and withdrawal are ordinary registrar operations; only
        the *academic meaning* of the enrollment is frozen."""
        enrollment = _enrollment(offered, status=EnrollmentStatus.ENROLLED)
        session.add(enrollment)
        session.flush()

        enrollment.section_id = offered.section.id
        session.flush()

        enrollment.status = EnrollmentStatus.DROPPED
        session.flush()

        assert enrollment.status is EnrollmentStatus.DROPPED
        assert enrollment.section_id == offered.section.id
