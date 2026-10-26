"""Constraint catalog and constraint instances.

SESSION 2 decides which constraints exist and how they are classified; this
module fixes only the *shape* those decisions are stored in.

Two rules are fixed here and are not SESSION 2's to relax:

* A `PlanningConstraint` may not weaken a constraint type whose catalog
  default is HARD unless a human authority explicitly recorded the
  exception. AI never does this (CLAUDE.md — Optimization principles).
* A `Preference` is not a constraint. Anything that must actually be
  enforced lives here, created by someone with the authority to impose it.
"""

from __future__ import annotations

import uuid
from typing import Any, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import ConstraintCategory
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
    from academic_ai.infrastructure.db.models.people import User


class ConstraintType(Base, TimestampMixin, ActorMixin):
    """Catalog entry describing a kind of constraint.

    ``parameter_schema`` is a JSON Schema for the instance ``parameters``,
    and ``parameter_schema_version`` is stored on every optimization run that
    used it — otherwise a past run's parameters become uninterpretable the
    first time the schema evolves.
    """

    __tablename__ = "constraint_type"
    __table_args__ = (UniqueConstraint("code", name="uq_constraint_type_code"),)

    id: Mapped[uuid.UUID] = pk_column()
    code: Mapped[str] = code_column(64)
    name: Mapped[str] = name_column()
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    default_category: Mapped[ConstraintCategory] = mapped_column(
        pg_enum(ConstraintCategory, "constraint_category"), nullable=False
    )
    #: False when an instance may not override `default_category` at all —
    #: used for the constraints that define physical reality (a room holds
    #: one class at a time) as opposed to institutional policy.
    is_category_overridable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    parameter_schema: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    parameter_schema_version: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default="1"
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    instances: Mapped[list[PlanningConstraint]] = relationship(
        back_populates="constraint_type"
    )


class PlanningConstraint(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """An instance of a constraint type, scoped to some part of the
    institution.

    Domain name is "Constraint"; the class is `PlanningConstraint` because
    ``Constraint`` is SQLAlchemy's own schema object.

    All scope columns NULL means institution-wide. The unique key uses
    ``NULLS NOT DISTINCT`` so that an institution-wide duplicate is actually
    rejected — with default NULL semantics it would not be.
    """

    __tablename__ = "planning_constraint"
    __table_args__ = (
        UniqueConstraint(
            "constraint_type_id",
            "academic_period_id",
            "scope_faculty_id",
            "scope_program_id",
            "scope_department_id",
            "scope_teacher_id",
            "scope_room_id",
            "scope_course_id",
            "scope_section_id",
            name="uq_planning_constraint_type_period_scope",
            postgresql_nulls_not_distinct=True,
        ),
        CheckConstraint("weight >= 0", name="weight_non_negative"),
        # Downgrading a HARD constraint is an institutional act and must name
        # the person who took it.
        CheckConstraint(
            "category_override IS NULL OR "
            "(overridden_by_user_id IS NOT NULL AND override_reason IS NOT NULL)",
            name="override_requires_authority",
        ),
        Index(
            "ix_planning_constraint_academic_period_id_is_active",
            "academic_period_id",
            "is_active",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    constraint_type_id: Mapped[uuid.UUID] = fk_column("constraint_type.id")
    academic_period_id: Mapped[uuid.UUID | None] = fk_column(
        "academic_period.id", nullable=True, ondelete="CASCADE"
    )

    # --- scope (all NULL = institution-wide) ---
    scope_faculty_id: Mapped[uuid.UUID | None] = fk_column("faculty.id", nullable=True)
    scope_program_id: Mapped[uuid.UUID | None] = fk_column("program.id", nullable=True)
    scope_department_id: Mapped[uuid.UUID | None] = fk_column(
        "department.id", nullable=True
    )
    scope_teacher_id: Mapped[uuid.UUID | None] = fk_column("teacher.id", nullable=True)
    scope_room_id: Mapped[uuid.UUID | None] = fk_column("room.id", nullable=True)
    scope_course_id: Mapped[uuid.UUID | None] = fk_column("course.id", nullable=True)
    scope_section_id: Mapped[uuid.UUID | None] = fk_column("section.id", nullable=True)

    parameters: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    #: NULL means "use the type's default_category". A non-NULL value is an
    #: explicit institutional override and requires an approver and a reason.
    category_override: Mapped[ConstraintCategory | None] = mapped_column(
        pg_enum(ConstraintCategory, "constraint_category"), nullable=True
    )
    overridden_by_user_id: Mapped[uuid.UUID | None] = fk_column(
        "app_user.id", nullable=True, ondelete="SET NULL"
    )
    override_reason: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    weight: Mapped[float] = mapped_column(
        Numeric(8, 3), nullable=False, server_default="1.0"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )

    constraint_type: Mapped[ConstraintType] = relationship(back_populates="instances")
    overridden_by: Mapped[User | None] = relationship(
        foreign_keys=[overridden_by_user_id]
    )
