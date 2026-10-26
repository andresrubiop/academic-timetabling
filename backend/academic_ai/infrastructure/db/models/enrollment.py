"""Enrollment — the authoritative record of student demand.

"Enrollment is authoritative for student conflicts" (playbook SESSION 1).
Everything the optimizer knows about which students collide comes from this
table; cohorts are a reporting convenience and never a conflict authority,
because two students in the same cohort routinely differ once one of them is
repeating a course.

An enrollment must stay interpretable for years. Curricula are versioned and
revised, so an enrollment that merely pointed at "the course" and "the
curriculum" would become unreadable the moment either changed: you could no
longer answer *which requirement this enrollment satisfied*, which is exactly
what SESSION 5's progression-risk and mandatory-status inputs need. The row
therefore snapshots its academic meaning at the moment of enrollment, and the
snapshot is immutable once the enrollment is confirmed.

See `docs/adr/0005-enrollment-immutable-record.md`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import EnrollmentKind, EnrollmentStatus
from academic_ai.infrastructure.db.base import (
    ActorMixin,
    Base,
    MetadataMixin,
    TimestampMixin,
    UUIDType,
    fk_column,
    pg_enum,
    pk_column,
)

if TYPE_CHECKING:
    from academic_ai.infrastructure.db.models.curriculum import (
        CurriculumCourse,
        CurriculumVersion,
    )
    from academic_ai.infrastructure.db.models.offering import CourseOffering, Section
    from academic_ai.infrastructure.db.models.people import Student

#: Columns frozen once the enrollment leaves REQUESTED. Enforced by the
#: `enrollment_snapshot_is_immutable` trigger created in the first migration;
#: named here so the trigger and the tests share one definition.
IMMUTABLE_SNAPSHOT_COLUMNS = (
    "student_id",
    "course_offering_id",
    "course_id",
    "academic_period_id",
    "curriculum_version_id",
    "curriculum_course_id",
    "is_mandatory",
    "enrollment_kind",
    "attempt_number",
)


class Enrollment(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A student's enrollment in one course offering for one period.

    ``course_offering_id`` is required — academic intent exists before
    sectioning. ``section_id`` is nullable and is filled by the registrar
    when the student is placed.

    **Sectioning is not an optimizer decision variable** (SESSION 2–5). If
    it ever becomes one, the placement must be version-scoped in its own
    entity and must never write back here: this row is the registrar's
    record, not a proposal. See
    `docs/adr/0009-sectioning-authority.md`.

    Snapshot columns (`course_id`, `academic_period_id`,
    `curriculum_version_id`, `curriculum_course_id`, `is_mandatory`,
    `enrollment_kind`, `attempt_number`) record what this enrollment *meant*
    academically. Composite foreign keys bind the denormalised copies to the
    offering and the section, so the database rejects an enrollment whose
    course or period disagrees with its offering, or whose section belongs to
    a different offering — cross-period and cross-offering corruption is
    structurally impossible rather than caught by review.
    """

    __tablename__ = "enrollment"
    __table_args__ = (
        UniqueConstraint(
            "student_id",
            "course_offering_id",
            name="uq_enrollment_student_id_course_offering_id",
        ),
        # The denormalised course and period must be the offering's own.
        ForeignKeyConstraint(
            ["course_offering_id", "course_id"],
            ["course_offering.id", "course_offering.course_id"],
            name="fk_enrollment_offering_course",
        ),
        ForeignKeyConstraint(
            ["course_offering_id", "academic_period_id"],
            ["course_offering.id", "course_offering.academic_period_id"],
            name="fk_enrollment_offering_period",
        ),
        # A section, when set, must belong to this enrollment's offering.
        ForeignKeyConstraint(
            ["section_id", "course_offering_id"],
            ["section.id", "section.course_offering_id"],
            name="fk_enrollment_section_offering",
        ),
        # A mandatory enrollment must name the curriculum requirement it
        # satisfies; otherwise "mandatory under which plan?" is unanswerable
        # and SESSION 5 progression risk cannot be computed.
        CheckConstraint(
            "NOT is_mandatory OR curriculum_course_id IS NOT NULL",
            name="mandatory_requires_curriculum_course",
        ),
        CheckConstraint("attempt_number >= 1", name="attempt_number_positive"),
        # A second or later attempt is by definition a repeat.
        CheckConstraint(
            "attempt_number = 1 OR enrollment_kind = 'REPEATED'",
            name="repeat_attempt_is_repeated_kind",
        ),
        # Hot path for SESSION 5: counting how full a section is, and hence
        # whether it is a genuine alternative for a student.
        Index(
            "ix_enrollment_section_id_active",
            "section_id",
            postgresql_where=text(
                "status IN ('REQUESTED', 'ENROLLED', 'WAITLISTED')"
            ),
        ),
        Index(
            "ix_enrollment_academic_period_id_student_id",
            "academic_period_id",
            "student_id",
        ),
        Index(
            "ix_enrollment_course_id_academic_period_id",
            "course_id",
            "academic_period_id",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    student_id: Mapped[uuid.UUID] = fk_column("student.id")
    course_offering_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, nullable=False, index=True
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType, nullable=True, index=True
    )

    # --- academic snapshot (immutable once confirmed) ---
    course_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    academic_period_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, nullable=False, index=True
    )
    curriculum_version_id: Mapped[uuid.UUID] = fk_column("curriculum_version.id")
    #: The specific curriculum requirement this enrollment satisfies. NULL is
    #: legitimate for a free elective that appears in no plan.
    curriculum_course_id: Mapped[uuid.UUID | None] = fk_column(
        "curriculum_course.id", nullable=True
    )
    is_mandatory: Mapped[bool] = mapped_column(Boolean, nullable=False)
    enrollment_kind: Mapped[EnrollmentKind] = mapped_column(
        pg_enum(EnrollmentKind, "enrollment_kind"), nullable=False
    )
    #: 1 = first attempt. > 1 implies `EnrollmentKind.REPEATED`.
    attempt_number: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="1"
    )

    status: Mapped[EnrollmentStatus] = mapped_column(
        pg_enum(EnrollmentStatus, "enrollment_status"),
        nullable=False,
        default=EnrollmentStatus.REQUESTED,
    )

    student: Mapped[Student] = relationship()
    course_offering: Mapped[CourseOffering] = relationship(
        foreign_keys=[course_offering_id, course_id],
        viewonly=True,
    )
    section: Mapped[Section | None] = relationship(
        foreign_keys=[section_id, course_offering_id],
        viewonly=True,
    )
    curriculum_version: Mapped[CurriculumVersion] = relationship()
    curriculum_course: Mapped[CurriculumCourse | None] = relationship()
