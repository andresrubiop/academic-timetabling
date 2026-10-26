"""Curriculum: the standing academic plan of a programme."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Date,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import LifecycleStatus, RequirementType
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
    from academic_ai.infrastructure.db.models.course import Course
    from academic_ai.infrastructure.db.models.organization import Program


class Curriculum(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A programme's plan of study, independent of any particular revision."""

    __tablename__ = "curriculum"
    __table_args__ = (
        UniqueConstraint("program_id", "code", name="uq_curriculum_program_id_code"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    program_id: Mapped[uuid.UUID] = fk_column("program.id")
    code: Mapped[str] = code_column(32)
    name: Mapped[str] = name_column()
    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, "lifecycle_status"),
        nullable=False,
        default=LifecycleStatus.ACTIVE,
    )

    program: Mapped[Program] = relationship()
    versions: Mapped[list[CurriculumVersion]] = relationship(
        back_populates="curriculum"
    )


class CurriculumVersion(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A dated revision of a curriculum.

    Versions exist because students are bound to the plan in force when they
    were admitted. A student repeating a course years later must still be
    evaluated against *their* version, which is why `Student` carries a
    ``curriculum_version_id`` and why `Enrollment` snapshots mandatory-ness
    rather than resolving it against whatever version is current today.
    """

    __tablename__ = "curriculum_version"
    __table_args__ = (
        UniqueConstraint(
            "curriculum_id", "version_number", name="uq_curriculum_version_curriculum_id_version_number"
        ),
        CheckConstraint("version_number > 0", name="version_number_positive"),
        CheckConstraint(
            "valid_until IS NULL OR valid_until > valid_from",
            name="valid_until_after_valid_from",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    curriculum_id: Mapped[uuid.UUID] = fk_column("curriculum.id")
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_from: Mapped[dt.date] = mapped_column(Date, nullable=False)
    valid_until: Mapped[dt.date | None] = mapped_column(Date, nullable=True)
    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, "lifecycle_status"),
        nullable=False,
        default=LifecycleStatus.DRAFT,
    )

    curriculum: Mapped[Curriculum] = relationship(back_populates="versions")
    levels: Mapped[list[CurriculumLevel]] = relationship(
        back_populates="curriculum_version", cascade="all, delete-orphan"
    )
    courses: Mapped[list[CurriculumCourse]] = relationship(
        back_populates="curriculum_version", cascade="all, delete-orphan"
    )


class CurriculumLevel(Base, TimestampMixin, MetadataMixin):
    """A level/semester within a curriculum version (1st, 2nd, ... n-th)."""

    __tablename__ = "curriculum_level"
    __table_args__ = (
        UniqueConstraint(
            "curriculum_version_id",
            "level_number",
            name="uq_curriculum_level_curriculum_version_id_level_number",
        ),
        CheckConstraint("level_number > 0", name="level_number_positive"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    curriculum_version_id: Mapped[uuid.UUID] = fk_column(
        "curriculum_version.id", ondelete="CASCADE"
    )
    level_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)

    curriculum_version: Mapped[CurriculumVersion] = relationship(
        back_populates="levels"
    )


class CurriculumCourse(Base, TimestampMixin, MetadataMixin):
    """A course's place in a curriculum version: which level, and whether it
    is mandatory.

    This is the *academic plan*. It is deliberately distinct from
    `CourseOfferingProgram`, which records which programmes a concrete
    offering actually serves in one period. A course can be planned here and
    not offered this period, or offered and served to a programme that does
    not have it in its plan (a service course taken as a free elective).
    """

    __tablename__ = "curriculum_course"
    __table_args__ = (
        UniqueConstraint(
            "curriculum_version_id",
            "course_id",
            name="uq_curriculum_course_curriculum_version_id_course_id",
        ),
        CheckConstraint(
            "credits IS NULL OR credits >= 0", name="credits_non_negative"
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    curriculum_version_id: Mapped[uuid.UUID] = fk_column(
        "curriculum_version.id", ondelete="CASCADE"
    )
    curriculum_level_id: Mapped[uuid.UUID] = fk_column("curriculum_level.id")
    course_id: Mapped[uuid.UUID] = fk_column("course.id")
    requirement_type: Mapped[RequirementType] = mapped_column(
        pg_enum(RequirementType, "requirement_type"), nullable=False
    )
    #: Overrides `Course.credits` when a programme values the course
    #: differently. NULL means "use the course's own credit value".
    credits: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    curriculum_version: Mapped[CurriculumVersion] = relationship(
        back_populates="courses"
    )
    curriculum_level: Mapped[CurriculumLevel] = relationship()
    course: Mapped[Course] = relationship()
