"""Courses, equivalences, and academic compatibility groups.

Three concepts are kept strictly apart here because conflating them is the
classic way academic scheduling systems become unfixable:

* **Course** — what is taught. Owned by a Department, never by a Program, so
  it can be shared.
* **CourseEquivalence** — "passing X satisfies the requirement Y". A
  *progression* statement. Never implies the two may be taught together.
* **AcademicCompatibilityGroup** — "these courses may be taught as one
  merged offering". A *scheduling* statement. Never implies credit transfer.

Both equivalence and compatibility carry an approval lifecycle, because
CLAUDE.md forbids AI from automatically approving academic equivalence or
cross-program/cross-faculty decisions.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import (
    AcademicApprovalStatus,
    CourseStatus,
    ProposalSource,
)
from academic_ai.infrastructure.db.base import (
    ActorMixin,
    Base,
    MetadataMixin,
    TimestampMixin,
    code_column,
    fk_column,
    name_column,
    pg_enum,
    pk_column,
)

if TYPE_CHECKING:
    from academic_ai.infrastructure.db.models.organization import (
        Department,
        Institution,
        Program,
    )
    from academic_ai.infrastructure.db.models.people import User

#: Reused by both approval-bearing tables. APPROVED is the only status the
#: optimizer may act on, and it requires a human approver and a timestamp.
_APPROVAL_REQUIRES_APPROVER = (
    "(status <> 'APPROVED') OR "
    "(approved_by_user_id IS NOT NULL AND approved_at IS NOT NULL)"
)


class Course(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A catalog course.

    Owned by a `Department`, deliberately *not* by a `Program`: a single
    "Calculus I" must be able to serve Engineering, Physics and Economics
    without any of them owning it. Programme membership is expressed through
    `CurriculumCourse` (the plan) and `CourseOfferingProgram` (the period's
    operational reality).
    """

    __tablename__ = "course"
    __table_args__ = (
        UniqueConstraint("institution_id", "code", name="uq_course_institution_id_code"),
        CheckConstraint("credits >= 0", name="credits_non_negative"),
        CheckConstraint("theory_hours >= 0", name="theory_hours_non_negative"),
        CheckConstraint("practice_hours >= 0", name="practice_hours_non_negative"),
        CheckConstraint("lab_hours >= 0", name="lab_hours_non_negative"),
        CheckConstraint(
            "theory_hours + practice_hours + lab_hours > 0",
            name="some_contact_hours",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    institution_id: Mapped[uuid.UUID] = fk_column("institution.id")
    department_id: Mapped[uuid.UUID] = fk_column("department.id")
    code: Mapped[str] = code_column(32)
    name: Mapped[str] = name_column()
    credits: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    theory_hours: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    practice_hours: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    lab_hours: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="0"
    )
    status: Mapped[CourseStatus] = mapped_column(
        pg_enum(CourseStatus, "course_status"),
        nullable=False,
        default=CourseStatus.DRAFT,
    )

    department: Mapped[Department] = relationship()


class CourseEquivalence(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """"Passing ``source_course`` satisfies the requirement ``target_course``".

    **Directional on purpose.** Real academic equivalence frequently is: a
    5-credit Calculus course may satisfy a 3-credit one without the converse
    holding. A genuinely mutual equivalence is two rows, which is explicit
    and independently revocable — revoking one direction must not silently
    revoke the other.

    Scope columns narrow the claim: an equivalence may hold only for one
    programme or only for one curriculum version. Both NULL means it holds
    institution-wide.

    **Equivalence is not transitive.** Only explicit APPROVED rows count;
    nothing may compute a transitive closure. If A→B and B→C were composed
    into A→C, two separately approved decisions would silently manufacture a
    third that no one approved — and since an AI may propose rows
    (``proposal_source = AI_SUGGESTION``), that is exactly the "AI
    automatically approves academic equivalence" failure CLAUDE.md forbids.
    """

    __tablename__ = "course_equivalence"
    __table_args__ = (
        # NULLS NOT DISTINCT is required, not cosmetic: with PostgreSQL's
        # default NULL semantics an institution-wide equivalence (both scope
        # columns NULL) could be inserted without limit, which for an
        # *approved academic equivalence* is a governance hole rather than a
        # data smell.
        UniqueConstraint(
            "source_course_id",
            "target_course_id",
            "scope_program_id",
            "scope_curriculum_version_id",
            name="uq_course_equivalence_source_target_scope",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint(
            "source_course_id <> target_course_id", name="no_self_equivalence"
        ),
        CheckConstraint(_APPROVAL_REQUIRES_APPROVER, name="approved_requires_approver"),
        Index(
            "ix_course_equivalence_target_course_id_status",
            "target_course_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    source_course_id: Mapped[uuid.UUID] = fk_column("course.id")
    target_course_id: Mapped[uuid.UUID] = fk_column("course.id")
    scope_program_id: Mapped[uuid.UUID | None] = fk_column(
        "program.id", nullable=True
    )
    scope_curriculum_version_id: Mapped[uuid.UUID | None] = fk_column(
        "curriculum_version.id", nullable=True
    )

    proposal_source: Mapped[ProposalSource] = mapped_column(
        pg_enum(ProposalSource, "proposal_source"),
        nullable=False,
        default=ProposalSource.HUMAN,
    )
    status: Mapped[AcademicApprovalStatus] = mapped_column(
        pg_enum(AcademicApprovalStatus, "academic_approval_status"),
        nullable=False,
        default=AcademicApprovalStatus.PROPOSED,
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = fk_column(
        "app_user.id", nullable=True, ondelete="SET NULL"
    )
    approved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rationale: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    source_course: Mapped[Course] = relationship(foreign_keys=[source_course_id])
    target_course: Mapped[Course] = relationship(foreign_keys=[target_course_id])
    scope_program: Mapped[Program | None] = relationship()
    approved_by: Mapped[User | None] = relationship(
        foreign_keys=[approved_by_user_id]
    )


class AcademicCompatibilityGroup(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A set of courses whose teaching may be merged into one offering.

    This is a *scheduling* authorisation, not a credit statement. Merging
    "Calculus I" from three programmes into one section is only legitimate
    once a human authority has approved this group; an AI may propose it
    (``proposal_source = AI_SUGGESTION``) but the row stays PROPOSED until a
    person approves it, and only APPROVED groups may back a merged
    `CourseOffering`.
    """

    __tablename__ = "academic_compatibility_group"
    __table_args__ = (
        UniqueConstraint(
            "institution_id", "code", name="uq_academic_compatibility_group_institution_id_code"
        ),
        CheckConstraint(_APPROVAL_REQUIRES_APPROVER, name="approved_requires_approver"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    institution_id: Mapped[uuid.UUID] = fk_column("institution.id")
    code: Mapped[str] = code_column(48)
    name: Mapped[str] = name_column()
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    proposal_source: Mapped[ProposalSource] = mapped_column(
        pg_enum(ProposalSource, "proposal_source"),
        nullable=False,
        default=ProposalSource.HUMAN,
    )
    status: Mapped[AcademicApprovalStatus] = mapped_column(
        pg_enum(AcademicApprovalStatus, "academic_approval_status"),
        nullable=False,
        default=AcademicApprovalStatus.PROPOSED,
    )
    approved_by_user_id: Mapped[uuid.UUID | None] = fk_column(
        "app_user.id", nullable=True, ondelete="SET NULL"
    )
    approved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    rejection_reason: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    members: Mapped[list[AcademicCompatibilityGroupMember]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )
    approved_by: Mapped[User | None] = relationship(
        foreign_keys=[approved_by_user_id]
    )


class AcademicCompatibilityGroupMember(Base, TimestampMixin):
    """Membership of a course in a compatibility group.

    ``is_canonical`` marks the course whose catalog entry represents the
    merged offering. Exactly one canonical member per group is enforced by a
    partial unique index.
    """

    __tablename__ = "academic_compatibility_group_member"
    __table_args__ = (
        UniqueConstraint(
            "group_id", "course_id", name="uq_academic_compatibility_group_member_group_id_course_id"
        ),
        Index(
            "uq_academic_compatibility_group_member_canonical",
            "group_id",
            unique=True,
            postgresql_where=text("is_canonical"),
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    group_id: Mapped[uuid.UUID] = fk_column(
        "academic_compatibility_group.id", ondelete="CASCADE"
    )
    course_id: Mapped[uuid.UUID] = fk_column("course.id")
    is_canonical: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    group: Mapped[AcademicCompatibilityGroup] = relationship(back_populates="members")
    course: Mapped[Course] = relationship()
