"""Physical resources: Campus, Building, Room, RoomFeature, TravelTime."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import LifecycleStatus, RoomType
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
    from academic_ai.infrastructure.db.models.organization import Institution


class Campus(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A physical site. Campus identity drives the travel-time constraint:
    two sessions a student or teacher must attend back-to-back on different
    campuses are infeasible unless the gap covers `TravelTime`."""

    __tablename__ = "campus"
    __table_args__ = (
        UniqueConstraint("institution_id", "code", name="uq_campus_institution_id_code"),
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

    institution: Mapped[Institution] = relationship(back_populates="campuses")
    buildings: Mapped[list[Building]] = relationship(back_populates="campus")


class Building(Base, TimestampMixin, ActorMixin, MetadataMixin):
    __tablename__ = "building"
    __table_args__ = (
        UniqueConstraint("campus_id", "code", name="uq_building_campus_id_code"),
        # Redundant alone (id is unique) but required as the target of the
        # composite FK that binds Room.campus_id to its building's campus.
        UniqueConstraint("id", "campus_id", name="uq_building_id_campus_id"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    campus_id: Mapped[uuid.UUID] = fk_column("campus.id")
    code: Mapped[str] = code_column(32)
    name: Mapped[str] = name_column()
    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, "lifecycle_status"),
        nullable=False,
        default=LifecycleStatus.ACTIVE,
    )

    campus: Mapped[Campus] = relationship(back_populates="buildings")
    rooms: Mapped[list[Room]] = relationship(back_populates="building")


class Room(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A schedulable space.

    ``campus_id`` is denormalised from ``building.campus_id`` so that campus
    filtering and travel-time lookup are single-hop in the CP-SAT model
    builder. The denormalisation cannot drift: a composite foreign key binds
    ``(building_id, campus_id)`` to ``building (id, campus_id)``, so a room
    whose campus disagrees with its building is rejected by the database.
    """

    __tablename__ = "room"
    __table_args__ = (
        UniqueConstraint("building_id", "code", name="uq_room_building_id_code"),
        ForeignKeyConstraint(
            ["building_id", "campus_id"],
            ["building.id", "building.campus_id"],
            name="fk_room_building_campus",
        ),
        CheckConstraint("capacity > 0", name="capacity_positive"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    building_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    campus_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    code: Mapped[str] = code_column(32)
    name: Mapped[str] = name_column()
    room_type: Mapped[RoomType] = mapped_column(
        pg_enum(RoomType, "room_type"), nullable=False
    )
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[LifecycleStatus] = mapped_column(
        pg_enum(LifecycleStatus, "lifecycle_status"),
        nullable=False,
        default=LifecycleStatus.ACTIVE,
    )

    building: Mapped[Building] = relationship(back_populates="rooms")
    feature_assignments: Mapped[list[RoomFeatureAssignment]] = relationship(
        back_populates="room", cascade="all, delete-orphan"
    )


class RoomFeature(Base, TimestampMixin, ActorMixin):
    """Catalog of capabilities a room may provide (projector, fume hood,
    30 workstations, ...). Sessions declare required features; the optimizer
    treats "required feature present in sufficient quantity" as HARD."""

    __tablename__ = "room_feature"
    __table_args__ = (
        UniqueConstraint("institution_id", "code", name="uq_room_feature_institution_id_code"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    institution_id: Mapped[uuid.UUID] = fk_column("institution.id")
    code: Mapped[str] = code_column(48)
    name: Mapped[str] = name_column()
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class RoomFeatureAssignment(Base, TimestampMixin):
    """Quantity of a feature available in a room."""

    __tablename__ = "room_feature_assignment"
    __table_args__ = (
        UniqueConstraint(
            "room_id", "room_feature_id", name="uq_room_feature_assignment_room_id_room_feature_id"
        ),
        CheckConstraint("quantity > 0", name="quantity_positive"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    room_id: Mapped[uuid.UUID] = fk_column("room.id", ondelete="CASCADE")
    room_feature_id: Mapped[uuid.UUID] = fk_column("room_feature.id")
    quantity: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")

    room: Mapped[Room] = relationship(back_populates="feature_assignments")
    feature: Mapped[RoomFeature] = relationship()


class TravelTime(Base, TimestampMixin, ActorMixin):
    """Minimum minutes required to travel between two campuses.

    Stored directionally (A->B and B->A are separate rows) because real
    travel times are not symmetric in general — one-way systems, shuttle
    timetables, and uphill walks all break symmetry. A self-pair is rejected:
    intra-campus movement is covered by the schedule's slot granularity.
    """

    __tablename__ = "travel_time"
    __table_args__ = (
        UniqueConstraint(
            "origin_campus_id",
            "destination_campus_id",
            name="uq_travel_time_origin_campus_id_destination_campus_id",
        ),
        CheckConstraint(
            "origin_campus_id <> destination_campus_id", name="no_self_travel"
        ),
        CheckConstraint("minutes >= 0", name="minutes_non_negative"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    origin_campus_id: Mapped[uuid.UUID] = fk_column("campus.id")
    destination_campus_id: Mapped[uuid.UUID] = fk_column("campus.id")
    minutes: Mapped[int] = mapped_column(Integer, nullable=False)

    origin_campus: Mapped[Campus] = relationship(foreign_keys=[origin_campus_id])
    destination_campus: Mapped[Campus] = relationship(
        foreign_keys=[destination_campus_id]
    )
