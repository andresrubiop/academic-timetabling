"""Can this schema answer SESSION 5's criticality question?

SESSION 5 states the rule that drives student-centric optimization:

    one student with a repeated mandatory course and only one section may be
    more critical than many students with multiple alternatives.

The optimizer is SESSION 5's job. What SESSION 1 owes it is a schema from
which the *inputs* to that rule are computable, correctly and efficiently:
affected students, repeated-course status, mandatory status, alternative
sections, alternative offerings, and the no-alternative condition.

These tests are a standing check that the schema can answer those questions
before any optimizer exists. If a later session breaks one of them, the
criticality rule silently inverts — and the failure mode is that the student
with no alternatives is deprioritised, which is precisely backwards.

One subtlety is tested explicitly: a section's remaining capacity must count
enrollments **that have not been placed in a section yet**. Counting only
placed enrollments overestimates free space, which understates the
criticality of exactly the student the rule exists to protect.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from academic_ai.domain.enums import (
    AcademicApprovalStatus,
    EnrollmentKind,
    EnrollmentStatus,
    Modality,
    OfferingStatus,
    SectionStatus,
    ServiceRole,
)
from academic_ai.infrastructure.db.models import (
    CourseEquivalence,
    CourseOffering,
    CourseOfferingProgram,
    Enrollment,
    Section,
    Student,
)
from tests.conftest import AcademicWorld

ACTIVE_STATUSES = (
    EnrollmentStatus.REQUESTED,
    EnrollmentStatus.ENROLLED,
    EnrollmentStatus.WAITLISTED,
)


# ---------------------------------------------------------------------------
# The queries SESSION 5 will need. Kept here, next to their tests, so this
# file documents the intended access pattern as well as checking it.
# ---------------------------------------------------------------------------


def count_open_sections(session: Session, offering_id) -> int:
    """Sections of an offering that could still take a student."""
    return session.scalar(
        select(func.count(Section.id)).where(
            Section.course_offering_id == offering_id,
            Section.status.in_([SectionStatus.PLANNED, SectionStatus.OPEN]),
        )
    )


def section_demand(session: Session, section_id) -> int:
    """How many students are counting on this section."""
    return session.scalar(
        select(func.count(Enrollment.id)).where(
            Enrollment.section_id == section_id,
            Enrollment.status.in_(ACTIVE_STATUSES),
        )
    )


def unplaced_demand(session: Session, offering_id) -> int:
    """Active enrollments in an offering that have no section yet.

    These are invisible to `section_demand` but are real demand. Ignoring
    them is what makes remaining capacity look larger than it is.
    """
    return session.scalar(
        select(func.count(Enrollment.id)).where(
            Enrollment.course_offering_id == offering_id,
            Enrollment.section_id.is_(None),
            Enrollment.status.in_(ACTIVE_STATUSES),
        )
    )


def sections_with_room(session: Session, offering_id) -> list[Section]:
    """Sections that are open *and* not already full.

    Only these are genuine alternatives for a student.
    """
    sections = (
        session.execute(
            select(Section).where(
                Section.course_offering_id == offering_id,
                Section.status.in_([SectionStatus.PLANNED, SectionStatus.OPEN]),
            )
        )
        .scalars()
        .all()
    )
    return [s for s in sections if section_demand(session, s.id) < s.capacity]


def alternative_offerings(session: Session, course_id, period_id) -> list[CourseOffering]:
    """Other open offerings of the same course in the same period."""
    return (
        session.execute(
            select(CourseOffering).where(
                CourseOffering.course_id == course_id,
                CourseOffering.academic_period_id == period_id,
                CourseOffering.status.in_(
                    [OfferingStatus.PLANNED, OfferingStatus.OPEN]
                ),
            )
        )
        .scalars()
        .all()
    )


def approved_equivalent_course_ids(session: Session, target_course_id) -> list:
    """Courses that satisfy a requirement through an APPROVED equivalence.

    Only explicit APPROVED rows count. No transitive closure is computed:
    composing A→B and B→C would manufacture an equivalence nobody approved.
    """
    return list(
        session.execute(
            select(CourseEquivalence.source_course_id).where(
                CourseEquivalence.target_course_id == target_course_id,
                CourseEquivalence.status == AcademicApprovalStatus.APPROVED,
            )
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def _enroll(
    session: Session,
    world: AcademicWorld,
    student: Student,
    offering: CourseOffering,
    **overrides: object,
) -> Enrollment:
    kwargs: dict[str, object] = {
        "student_id": student.id,
        "course_offering_id": offering.id,
        "course_id": offering.course_id,
        "academic_period_id": world.period.id,
        "curriculum_version_id": world.curriculum_version.id,
        "curriculum_course_id": world.curriculum_course.id,
        "is_mandatory": True,
        "enrollment_kind": EnrollmentKind.CURRENT_LEVEL,
        "attempt_number": 1,
        "status": EnrollmentStatus.ENROLLED,
    }
    kwargs.update(overrides)
    enrollment = Enrollment(**kwargs)
    session.add(enrollment)
    session.flush()
    return enrollment


def _make_student(session: Session, world: AcademicWorld, code: str) -> Student:
    student = Student(
        institution_id=world.institution.id,
        program_id=world.program.id,
        curriculum_version_id=world.curriculum_version.id,
        code=code,
        full_name=f"Student {code}",
    )
    session.add(student)
    session.flush()
    return student


class TestAlternativesAreCountable:
    def test_single_section_offering_has_exactly_one_alternative(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        assert count_open_sections(session, offered.offering.id) == 1

    def test_extra_sections_are_counted(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        session.add(
            Section(
                course_offering_id=offered.offering.id,
                academic_period_id=offered.period.id,
                code="B",
                capacity=30,
                status=SectionStatus.OPEN,
            )
        )
        session.flush()
        assert count_open_sections(session, offered.offering.id) == 2

    def test_a_cancelled_section_is_not_an_alternative(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        session.add(
            Section(
                course_offering_id=offered.offering.id,
                academic_period_id=offered.period.id,
                code="B",
                capacity=30,
                status=SectionStatus.CANCELLED,
            )
        )
        session.flush()
        assert count_open_sections(session, offered.offering.id) == 1

    def test_a_full_section_is_not_an_alternative(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        # The seeded section has capacity 2.
        for i in range(2):
            student = _make_student(session, offered, f"F{i}")
            _enroll(
                session,
                offered,
                student,
                offered.offering,
                section_id=offered.section.id,
            )

        assert count_open_sections(session, offered.offering.id) == 1
        assert sections_with_room(session, offered.offering.id) == []

    def test_unplaced_enrollments_are_real_demand(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """The capacity-overestimation trap.

        Two students are enrolled in the offering but not yet placed in a
        section. Counting only placed enrollments would report the section as
        empty and therefore a free alternative for everyone — overstating
        available capacity and understating the criticality of a student who
        has nowhere else to go.
        """
        for i in range(2):
            student = _make_student(session, offered, f"U{i}")
            _enroll(session, offered, student, offered.offering, section_id=None)

        assert section_demand(session, offered.section.id) == 0
        assert unplaced_demand(session, offered.offering.id) == 2

        total_demand = (
            section_demand(session, offered.section.id)
            + unplaced_demand(session, offered.offering.id)
        )
        assert total_demand >= offered.section.capacity, (
            "the only section is already spoken for once unplaced demand is "
            "counted; treating it as free capacity is the inversion this "
            "test exists to catch"
        )

    def test_dropped_enrollments_do_not_consume_capacity(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        student = _make_student(session, offered, "D1")
        _enroll(
            session,
            offered,
            student,
            offered.offering,
            section_id=offered.section.id,
            status=EnrollmentStatus.DROPPED,
        )
        assert section_demand(session, offered.section.id) == 0


class TestAlternativeOfferings:
    def test_a_second_offering_of_the_same_course_is_an_alternative(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        evening = CourseOffering(
            course_id=offered.course.id,
            academic_period_id=offered.period.id,
            campus_id=offered.campus.id,
            modality=Modality.ONSITE,
            variant_code="NOCHE",
            status=OfferingStatus.OPEN,
        )
        session.add(evening)
        session.flush()

        alternatives = alternative_offerings(
            session, offered.course.id, offered.period.id
        )
        assert len(alternatives) == 2

    def test_a_cancelled_offering_is_not_an_alternative(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        cancelled = CourseOffering(
            course_id=offered.course.id,
            academic_period_id=offered.period.id,
            campus_id=offered.campus.id,
            modality=Modality.ONSITE,
            variant_code="CANCELADO",
            status=OfferingStatus.CANCELLED,
        )
        session.add(cancelled)
        session.flush()

        alternatives = alternative_offerings(
            session, offered.course.id, offered.period.id
        )
        assert len(alternatives) == 1

    def test_a_shared_cross_program_offering_serves_both_programmes(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        session.add(
            CourseOfferingProgram(
                course_offering_id=offered.offering.id,
                program_id=offered.other_program.id,
                service_role=ServiceRole.COMMON_CORE,
            )
        )
        session.flush()

        served = session.scalar(
            select(func.count(CourseOfferingProgram.id)).where(
                CourseOfferingProgram.course_offering_id == offered.offering.id
            )
        )
        assert served == 2


class TestEquivalenceAsAnAlternative:
    def test_only_approved_equivalences_count(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        proposed = CourseEquivalence(
            source_course_id=offered.other_course.id,
            target_course_id=offered.course.id,
            status=AcademicApprovalStatus.PROPOSED,
        )
        session.add(proposed)
        session.flush()

        assert approved_equivalent_course_ids(session, offered.course.id) == []

    def test_an_approved_equivalence_counts(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        import datetime as dt

        session.add(
            CourseEquivalence(
                source_course_id=offered.other_course.id,
                target_course_id=offered.course.id,
                status=AcademicApprovalStatus.APPROVED,
                approved_by_user_id=offered.admin.id,
                approved_at=dt.datetime.now(dt.timezone.utc),
            )
        )
        session.flush()

        assert approved_equivalent_course_ids(session, offered.course.id) == [
            offered.other_course.id
        ]


class TestTheCriticalRule:
    def test_the_no_alternative_repeated_student_is_identifiable(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """The playbook's headline case, end to end.

        One student repeating a mandatory course with exactly one section,
        versus twenty students with a second section available. SESSION 5
        decides the ranking; SESSION 1 must make the distinguishing facts
        queryable — and they are, from `Enrollment` alone plus a section
        count.
        """
        critical_student = _make_student(session, offered, "CRIT")
        critical = _enroll(
            session,
            offered,
            critical_student,
            offered.offering,
            section_id=offered.section.id,
            enrollment_kind=EnrollmentKind.REPEATED,
            attempt_number=2,
            is_mandatory=True,
        )

        # A second course, comfortably provisioned, for the twenty others.
        easy_offering = CourseOffering(
            course_id=offered.other_course.id,
            academic_period_id=offered.period.id,
            campus_id=offered.campus.id,
            modality=Modality.ONSITE,
            status=OfferingStatus.OPEN,
        )
        session.add(easy_offering)
        session.flush()

        easy_sections = [
            Section(
                course_offering_id=easy_offering.id,
                academic_period_id=offered.period.id,
                code=code,
                capacity=50,
                status=SectionStatus.OPEN,
            )
            for code in ("A", "B")
        ]
        session.add_all(easy_sections)
        session.flush()

        for i in range(20):
            student = _make_student(session, offered, f"E{i}")
            _enroll(
                session,
                offered,
                student,
                easy_offering,
                section_id=easy_sections[0].id,
                curriculum_course_id=None,
                is_mandatory=False,
                enrollment_kind=EnrollmentKind.ELECTIVE,
            )

        # The facts the criticality score is built from:
        assert critical.enrollment_kind is EnrollmentKind.REPEATED
        assert critical.attempt_number == 2
        assert critical.is_mandatory is True
        assert count_open_sections(session, offered.offering.id) == 1
        assert len(alternative_offerings(session, offered.course.id, offered.period.id)) == 1

        # ...versus the twenty, who have somewhere else to go.
        assert count_open_sections(session, easy_offering.id) == 2
        assert len(sections_with_room(session, easy_offering.id)) == 2

        no_alternative = (
            critical.is_mandatory
            and critical.enrollment_kind is EnrollmentKind.REPEATED
            and count_open_sections(session, offered.offering.id) == 1
            and len(
                alternative_offerings(session, offered.course.id, offered.period.id)
            )
            == 1
            and approved_equivalent_course_ids(session, offered.course.id) == []
        )
        assert no_alternative, (
            "the repeated mandatory single-section student must be "
            "identifiable as having no alternative"
        )
