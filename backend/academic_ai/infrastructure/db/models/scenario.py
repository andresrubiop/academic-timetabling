"""Scenarios — isolated what-if planning.

A scenario is a **declarative overlay**, never a mutation of base data.

Half of the perturbations the platform must support ("teacher unavailable",
"room unavailable", "new section", "change room requirement", "change campus
availability") change base tables rather than assignments. If a scenario were
only a draft `ScheduleVersion`, a hypothetical "this teacher is ill next
week" would have nowhere to live except the real `availability` table — and
SESSION 10 creates a scenario on *every drag-and-drop*.

So the rule, fixed here before the optimizer exists: **the problem builder's
input is `(base data, overlay)`, never the database alone.** A problem
builder that reads the database directly cannot be retrofitted with an
overlay without being rewritten. See
`docs/adr/0007-scenarios-as-overlays.md`.

Perturbations are typed rows rather than opaque JSON because SESSION 10 must
report affected teachers, rooms and programmes *before* solving, and
SESSION 12 must turn a scenario into a `ChangeRequest` carrying an impact
analysis. Neither is possible against untyped JSON with no foreign keys.
"""

from __future__ import annotations

import uuid
from typing import Any, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import ScenarioStatus
from academic_ai.infrastructure.db.base import (
    ActorMixin,
    Base,
    MetadataMixin,
    TimestampMixin,
    fk_column,
    name_column,
    pg_enum,
    pk_column,
)

if TYPE_CHECKING:
    from academic_ai.infrastructure.db.models.schedule import ScheduleVersion

#: Perturbation kinds the platform must support (playbook SESSION 11).
#: Stored as text with a CHECK rather than a native enum, because SESSION 11
#: will extend this list and a CHECK is a one-line migration.
SCENARIO_PERTURBATION_KINDS = (
    "MOVE_SESSION",
    "TEACHER_UNAVAILABLE",
    "ROOM_UNAVAILABLE",
    "NEW_SECTION",
    "MERGE_COMPATIBLE_OFFERINGS",
    "SPLIT_CO_TEACHING_GROUP",
    "CHANGE_INSTRUCTOR",
    "CHANGE_ROOM_REQUIREMENT",
    "CHANGE_CAMPUS_AVAILABILITY",
    "LOCK_ASSIGNMENT",
    "UNLOCK_ASSIGNMENT",
)


class Scenario(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """An isolated what-if built on top of an accepted schedule version.

    ``result_schedule_version_id`` holds the DRAFT version produced by
    solving the scenario, so diffing a scenario against its base reuses the
    same ScheduleDiff code path as any other pair of versions.
    """

    __tablename__ = "scenario"
    __table_args__ = (
        UniqueConstraint(
            "base_schedule_version_id", "code", name="uq_scenario_base_version_code"
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    base_schedule_version_id: Mapped[uuid.UUID] = fk_column(
        "schedule_version.id", ondelete="CASCADE"
    )
    result_schedule_version_id: Mapped[uuid.UUID | None] = fk_column(
        "schedule_version.id", nullable=True, ondelete="SET NULL"
    )
    code: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = name_column()
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    status: Mapped[ScenarioStatus] = mapped_column(
        pg_enum(ScenarioStatus, "scenario_status"),
        nullable=False,
        default=ScenarioStatus.OPEN,
    )

    base_schedule_version: Mapped[ScheduleVersion] = relationship(
        foreign_keys=[base_schedule_version_id]
    )
    result_schedule_version: Mapped[ScheduleVersion | None] = relationship(
        foreign_keys=[result_schedule_version_id]
    )
    perturbations: Mapped[list[ScenarioPerturbation]] = relationship(
        back_populates="scenario",
        cascade="all, delete-orphan",
        order_by="ScenarioPerturbation.sequence_number",
    )


class ScenarioPerturbation(Base, TimestampMixin, ActorMixin):
    """One declarative change the scenario applies on top of base data.

    Targets are real foreign keys, not identifiers buried in JSON, so
    "which teachers does this scenario affect?" is an ordinary query and a
    perturbation pointing at a deleted room is rejected by the database.
    ``payload`` carries only the kind-specific values that are not
    references (a new capacity, a proposed slot index).
    """

    __tablename__ = "scenario_perturbation"
    __table_args__ = (
        UniqueConstraint(
            "scenario_id",
            "sequence_number",
            name="uq_scenario_perturbation_scenario_id_sequence_number",
        ),
        CheckConstraint(
            "kind IN ("
            + ", ".join(f"'{kind}'" for kind in SCENARIO_PERTURBATION_KINDS)
            + ")",
            name="kind_is_known",
        ),
        CheckConstraint("sequence_number >= 0", name="sequence_number_non_negative"),
        Index("ix_scenario_perturbation_teacher_id", "teacher_id"),
        Index("ix_scenario_perturbation_room_id", "room_id"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    scenario_id: Mapped[uuid.UUID] = fk_column("scenario.id", ondelete="CASCADE")
    #: Perturbations are applied in this order; later ones may depend on
    #: entities created by earlier ones.
    sequence_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)

    # --- targets (which apply depends on `kind`) ---
    class_session_id: Mapped[uuid.UUID | None] = fk_column(
        "class_session.id", nullable=True, ondelete="CASCADE"
    )
    section_id: Mapped[uuid.UUID | None] = fk_column(
        "section.id", nullable=True, ondelete="CASCADE"
    )
    course_offering_id: Mapped[uuid.UUID | None] = fk_column(
        "course_offering.id", nullable=True, ondelete="CASCADE"
    )
    teacher_id: Mapped[uuid.UUID | None] = fk_column(
        "teacher.id", nullable=True, ondelete="CASCADE", index=False
    )
    room_id: Mapped[uuid.UUID | None] = fk_column(
        "room.id", nullable=True, ondelete="CASCADE", index=False
    )
    campus_id: Mapped[uuid.UUID | None] = fk_column(
        "campus.id", nullable=True, ondelete="CASCADE"
    )
    timeslot_id: Mapped[uuid.UUID | None] = fk_column(
        "timeslot.id", nullable=True, ondelete="CASCADE"
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    scenario: Mapped[Scenario] = relationship(back_populates="perturbations")
