"""Institutional structure: Institution -> Faculty -> Department -> Program."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import LifecycleStatus
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
    from academic_ai.infrastructure.db.models.facilities import Campus


class Institution(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """The university. Single-row in most deployments, but modelled as an
    entity so a multi-institution deployment does not need a migration."""

    __tablename__ = "institution"
    __table_args__ = (UniqueConstraint("code", name="uq_institution_code"),)

    id: Mapped[uuid.UUID] = pk_column()
    code: Mapped[str] = code_column(32)
    name: Mapped[str] = name_column()
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="UTC"
    )
    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, "lifecycle_status"),
        nullable=False,
        default=LifecycleStatus.ACTIVE,
    )

    faculties: Mapped[list[Faculty]] = relationship(back_populates="institution")
    campuses: Mapped[list[Campus]] = relationship(back_populates="institution")


class Faculty(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A faculty/school. Cross-faculty changes require institutional
    authority (CLAUDE.md — Human authority), which is why faculty identity is
    a first-class scope on schedules, constraints and approval workflows."""

    __tablename__ = "faculty"
    __table_args__ = (
        UniqueConstraint("institution_id", "code", name="uq_faculty_institution_id_code"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    institution_id: Mapped[uuid.UUID] = fk_column("institution.id")
    code: Mapped[str] = code_column(32)
    name: Mapped[str] = name_column()
    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, "lifecycle_status"),
        nullable=False,
        default=LifecycleStatus.ACTIVE,
    )

    institution: Mapped[Institution] = relationship(back_populates="faculties")
    departments: Mapped[list[Department]] = relationship(back_populates="faculty")
    programs: Mapped[list[Program]] = relationship(back_populates="faculty")


class Department(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """Owns courses and employs teachers.

    Course ownership sits on Department rather than Program precisely so a
    course can be shared across programs without any program "owning" it.
    """

    __tablename__ = "department"
    __table_args__ = (
        UniqueConstraint("faculty_id", "code", name="uq_department_faculty_id_code"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    faculty_id: Mapped[uuid.UUID] = fk_column("faculty.id")
    code: Mapped[str] = code_column(32)
    name: Mapped[str] = name_column()
    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, "lifecycle_status"),
        nullable=False,
        default=LifecycleStatus.ACTIVE,
    )

    faculty: Mapped[Faculty] = relationship(back_populates="departments")


class Program(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A degree programme (carrera). Students belong to exactly one program;
    curricula belong to a program."""

    __tablename__ = "program"
    __table_args__ = (
        UniqueConstraint("faculty_id", "code", name="uq_program_faculty_id_code"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    faculty_id: Mapped[uuid.UUID] = fk_column("faculty.id")
    department_id: Mapped[uuid.UUID | None] = fk_column("department.id", nullable=True)
    code: Mapped[str] = code_column(32)
    name: Mapped[str] = name_column()
    degree_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, "lifecycle_status"),
        nullable=False,
        default=LifecycleStatus.ACTIVE,
    )

    faculty: Mapped[Faculty] = relationship(back_populates="programs")
