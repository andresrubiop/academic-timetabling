"""People: User, Teacher, Student, StudentCohort."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import (
    LifecycleStatus,
    TeacherContractType,
    UserRole,
)
from academic_ai.infrastructure.db.base import (
    ActorMixin,
    Base,
    MetadataMixin,
    TimestampMixin,
    UUIDType,
    code_column,
    fk_column,
    name_column,
    pg_enum,
    pk_column,
)

if TYPE_CHECKING:
    from academic_ai.infrastructure.db.models.curriculum import (
        CurriculumLevel,
        CurriculumVersion,
    )
    from academic_ai.infrastructure.db.models.organization import Department, Program


class User(Base, TimestampMixin, MetadataMixin):
    """An authenticated actor.

    Exists in SESSION 1 (ahead of authentication, which is SESSION 8) because
    approvals, audit entries and AI-model activation all need a non-nullable
    notion of *who did this*, and retrofitting an actor into an audit trail
    after the fact produces an audit trail nobody can trust.

    Credentials (password hash, tokens, MFA) are deliberately NOT here yet —
    SESSION 8 owns them.
    """

    __tablename__ = "app_user"
    __table_args__ = (UniqueConstraint("email", name="uq_app_user_email"),)

    id: Mapped[uuid.UUID] = pk_column()
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    full_name: Mapped[str] = name_column()
    primary_role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )


class Teacher(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A teaching resource.

    ``max_weekly_hours`` and ``max_daily_hours`` feed the workload and
    daily-limit constraints (playbook SESSION 2). They are nullable: an
    unset limit means "no institutional limit recorded", which the optimizer
    must treat as unconstrained rather than as zero.
    """

    __tablename__ = "teacher"
    __table_args__ = (
        UniqueConstraint("institution_id", "code", name="uq_teacher_institution_id_code"),
        CheckConstraint(
            "max_weekly_hours IS NULL OR max_weekly_hours > 0",
            name="max_weekly_hours_positive",
        ),
        CheckConstraint(
            "max_daily_hours IS NULL OR max_daily_hours > 0",
            name="max_daily_hours_positive",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    institution_id: Mapped[uuid.UUID] = fk_column("institution.id")
    department_id: Mapped[uuid.UUID | None] = fk_column("department.id", nullable=True)
    user_id: Mapped[uuid.UUID | None] = fk_column("app_user.id", nullable=True)
    code: Mapped[str] = code_column(32)
    full_name: Mapped[str] = name_column()
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    contract_type: Mapped[TeacherContractType] = mapped_column(
        pg_enum(TeacherContractType, "teacher_contract_type"), nullable=False
    )
    max_weekly_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_daily_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, "lifecycle_status"),
        nullable=False,
        default=LifecycleStatus.ACTIVE,
    )

    department: Mapped[Department | None] = relationship()
    # `foreign_keys` is required on every relationship to `app_user`: the
    # ActorMixin already contributes created_by/updated_by FKs to the same
    # table, so the join is otherwise ambiguous.
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])


class Student(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A student.

    ``program_id`` is the student's home programme. It is not the authority
    for scheduling conflicts — `Enrollment` is (playbook SESSION 1/5) — but
    it is what makes "progression risk" and cross-program impact reportable.
    """

    __tablename__ = "student"
    __table_args__ = (
        UniqueConstraint("institution_id", "code", name="uq_student_institution_id_code"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    institution_id: Mapped[uuid.UUID] = fk_column("institution.id")
    program_id: Mapped[uuid.UUID] = fk_column("program.id")
    curriculum_version_id: Mapped[uuid.UUID | None] = fk_column(
        "curriculum_version.id", nullable=True
    )
    user_id: Mapped[uuid.UUID | None] = fk_column("app_user.id", nullable=True)
    code: Mapped[str] = code_column(32)
    full_name: Mapped[str] = name_column()
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    admission_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, "lifecycle_status"),
        nullable=False,
        default=LifecycleStatus.ACTIVE,
    )

    program: Mapped[Program] = relationship()
    curriculum_version: Mapped[CurriculumVersion | None] = relationship()
    user: Mapped[User | None] = relationship(foreign_keys=[user_id])


class StudentCohort(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A group of students who progress together through a curriculum level
    in one academic period (a "paralelo").

    A cohort is a planning convenience, never a conflict authority: two
    students in the same cohort may still hold different enrollments because
    one of them is repeating a course. The optimizer therefore reads
    `Enrollment`, and uses cohorts only for grouping and reporting.
    """

    __tablename__ = "student_cohort"
    __table_args__ = (
        UniqueConstraint(
            "academic_period_id",
            "curriculum_version_id",
            "curriculum_level_id",
            "code",
            name="uq_student_cohort_period_curriculum_level_code",
        ),
        # Redundant on its own (id is already unique), but required as the
        # target of the composite FK on student_cohort_membership that keeps
        # the denormalised period honest.
        UniqueConstraint(
            "id", "academic_period_id", name="uq_student_cohort_id_academic_period_id"
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    academic_period_id: Mapped[uuid.UUID] = fk_column("academic_period.id")
    program_id: Mapped[uuid.UUID] = fk_column("program.id")
    curriculum_version_id: Mapped[uuid.UUID] = fk_column("curriculum_version.id")
    curriculum_level_id: Mapped[uuid.UUID] = fk_column("curriculum_level.id")
    code: Mapped[str] = code_column(32)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expected_size: Mapped[int | None] = mapped_column(Integer, nullable=True)

    program: Mapped[Program] = relationship()
    curriculum_version: Mapped[CurriculumVersion] = relationship()
    curriculum_level: Mapped[CurriculumLevel] = relationship()
    memberships: Mapped[list[StudentCohortMembership]] = relationship(
        back_populates="cohort", cascade="all, delete-orphan"
    )


class StudentCohortMembership(Base, TimestampMixin):
    """A student's membership of a cohort.

    A student may belong to at most one cohort per academic period. The
    period is denormalised onto this row so that rule is a plain unique
    index rather than a cross-table trigger; a composite foreign key back to
    ``student_cohort (id, academic_period_id)`` stops the denormalised value
    from drifting away from the cohort's real period.
    """

    __tablename__ = "student_cohort_membership"
    __table_args__ = (
        UniqueConstraint(
            "student_id",
            "academic_period_id",
            name="uq_student_cohort_membership_student_id_academic_period_id",
        ),
        ForeignKeyConstraint(
            ["student_cohort_id", "academic_period_id"],
            ["student_cohort.id", "student_cohort.academic_period_id"],
            name="fk_student_cohort_membership_cohort_period",
            ondelete="CASCADE",
        ),
        Index(
            "ix_student_cohort_membership_student_cohort_id_student_id",
            "student_cohort_id",
            "student_id",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    student_id: Mapped[uuid.UUID] = fk_column("student.id")
    student_cohort_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, nullable=False, index=True
    )
    academic_period_id: Mapped[uuid.UUID] = fk_column("academic_period.id")

    student: Mapped[Student] = relationship()
    cohort: Mapped[StudentCohort] = relationship(back_populates="memberships")
