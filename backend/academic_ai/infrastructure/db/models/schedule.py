"""Schedules, versions and assignments.

The load-bearing decision here is **what one feasible schedule means**.

A `Schedule` may be scoped to a faculty or a programme, but that scope is
*authorship and filtering*, not an independent feasibility universe. Rooms,
teachers and students are shared across faculties — that sharing is the whole
premise of the product — so two independently published faculty schedules
would happily put two classes in the same room and CP-SAT would never see the
conflict, because it is only ever handed one scope at a time.

Publication is therefore institution-and-period-wide: at most one PUBLISHED
`ScheduleVersion` per ``(institution, academic_period)``. Faculty- and
programme-scoped drafts remain useful for planning and for routing change
requests; only *publication* is centralised. See
`docs/adr/0003-feasibility-and-publication-scope.md`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import (
    DayOfWeek,
    ScheduleScope,
    ScheduleVersionStatus,
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
    from academic_ai.infrastructure.db.models.facilities import Room
    from academic_ai.infrastructure.db.models.offering import ClassSession
    from academic_ai.infrastructure.db.models.people import User
    from academic_ai.infrastructure.db.models.period import Timeslot


class Schedule(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A planning artifact for one period, owned by some part of the
    institution.

    ``scope`` records *who is planning*, so change requests can be routed
    (SESSION 12: within-program vs cross-program vs cross-faculty) and so
    users see their own plan. It does not partition resources.
    """

    __tablename__ = "schedule"
    __table_args__ = (
        UniqueConstraint(
            "institution_id",
            "academic_period_id",
            "scope",
            "scope_faculty_id",
            "scope_program_id",
            "code",
            name="uq_schedule_institution_period_scope_code",
            postgresql_nulls_not_distinct=True,
        ),
        # The scope enum and the scope FKs must agree.
        CheckConstraint(
            "(scope = 'INSTITUTION' AND scope_faculty_id IS NULL AND scope_program_id IS NULL) OR "
            "(scope = 'FACULTY' AND scope_faculty_id IS NOT NULL AND scope_program_id IS NULL) OR "
            "(scope = 'PROGRAM' AND scope_program_id IS NOT NULL AND scope_faculty_id IS NULL)",
            name="scope_matches_scope_reference",
        ),
        UniqueConstraint("id", "institution_id", name="uq_schedule_id_institution_id"),
        UniqueConstraint(
            "id", "academic_period_id", name="uq_schedule_id_academic_period_id"
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    institution_id: Mapped[uuid.UUID] = fk_column("institution.id")
    academic_period_id: Mapped[uuid.UUID] = fk_column("academic_period.id")
    scope: Mapped[ScheduleScope] = mapped_column(
        pg_enum(ScheduleScope, "schedule_scope"), nullable=False
    )
    scope_faculty_id: Mapped[uuid.UUID | None] = fk_column("faculty.id", nullable=True)
    scope_program_id: Mapped[uuid.UUID | None] = fk_column("program.id", nullable=True)
    code: Mapped[str] = code_column(48)
    name: Mapped[str] = name_column()

    # ScheduleVersion reaches `schedule` through two composite foreign keys
    # (one carrying institution_id, one carrying academic_period_id), so the
    # join must be stated explicitly. Read-only: versions are created
    # through ScheduleVersion, never by appending here.
    versions: Mapped[list[ScheduleVersion]] = relationship(
        back_populates="schedule",
        primaryjoin="Schedule.id == ScheduleVersion.schedule_id",
        foreign_keys="ScheduleVersion.schedule_id",
        viewonly=True,
    )


class ScheduleVersion(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """One revision of a schedule.

    Only DRAFT versions may have their assignments edited; anything beyond
    DRAFT is append-only, enforced by the
    ``schedule_assignment_version_is_mutable`` trigger rather than by
    convention. A new plan is a new version with ``parent_version_id`` set,
    which is what makes ScheduleDiff and minimal-disruption reoptimisation
    (SESSION 11) possible at all.
    """

    __tablename__ = "schedule_version"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "version_number",
            name="uq_schedule_version_schedule_id_version_number",
        ),
        # F1: at most ONE published version per institution *and period* —
        # not per schedule. This is what makes "the published timetable"
        # a single, globally consistent object.
        Index(
            "uq_schedule_version_one_published_per_institution_period",
            "institution_id",
            "academic_period_id",
            unique=True,
            postgresql_where=text("status = 'PUBLISHED'"),
        ),
        CheckConstraint(
            "(status <> 'PUBLISHED') OR "
            "(published_at IS NOT NULL AND published_by_user_id IS NOT NULL)",
            name="published_requires_publisher",
        ),
        CheckConstraint("version_number > 0", name="version_number_positive"),
        ForeignKeyConstraint(
            ["schedule_id", "institution_id"],
            ["schedule.id", "schedule.institution_id"],
            name="fk_schedule_version_schedule_institution",
        ),
        ForeignKeyConstraint(
            ["schedule_id", "academic_period_id"],
            ["schedule.id", "schedule.academic_period_id"],
            name="fk_schedule_version_schedule_period",
        ),
        UniqueConstraint(
            "id", "academic_period_id", name="uq_schedule_version_id_academic_period_id"
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    schedule_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    #: Denormalised from `schedule` and bound by composite FK, so the
    #: one-published-per-period index can exist without a join.
    institution_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False)
    academic_period_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, nullable=False, index=True
    )
    version_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    status: Mapped[ScheduleVersionStatus] = mapped_column(
        pg_enum(ScheduleVersionStatus, "schedule_version_status"),
        nullable=False,
        default=ScheduleVersionStatus.DRAFT,
    )
    parent_version_id: Mapped[uuid.UUID | None] = fk_column(
        "schedule_version.id", nullable=True, ondelete="SET NULL"
    )
    #: Which run produced this version, if any. `optimization_run` points
    #: back at `schedule_version` (its base version), so this is a genuine
    #: cycle between two tables that legitimately reference each other;
    #: `use_alter` adds this constraint after both tables exist.
    optimization_run_id: Mapped[uuid.UUID | None] = fk_column(
        "optimization_run.id", nullable=True, ondelete="SET NULL", use_alter=True
    )
    published_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_by_user_id: Mapped[uuid.UUID | None] = fk_column(
        "app_user.id", nullable=True, ondelete="SET NULL"
    )
    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    schedule: Mapped[Schedule] = relationship(
        back_populates="versions",
        primaryjoin="Schedule.id == ScheduleVersion.schedule_id",
        foreign_keys="ScheduleVersion.schedule_id",
        viewonly=True,
    )
    parent_version: Mapped[ScheduleVersion | None] = relationship(
        remote_side="ScheduleVersion.id", foreign_keys=[parent_version_id]
    )
    published_by: Mapped[User | None] = relationship(
        foreign_keys=[published_by_user_id]
    )
    assignments: Mapped[list[ScheduleAssignment]] = relationship(
        back_populates="schedule_version", cascade="all, delete-orphan"
    )


class ScheduleAssignment(Base, TimestampMixin, ActorMixin):
    """Where and when one `ClassSession` happens, in one version.

    The time fields are denormalised from the timeslot and the session and
    bound back by composite foreign keys, for one reason: it makes room
    double-booking a *database exclusion constraint* rather than something a
    validator has to notice. A room cannot be occupied twice in the same
    version — not by a bug, not by a race, not by a bad import.

    Student and teacher overlap remain validator concerns (SESSION 3): they
    need joins through enrollment and session_teacher and cannot be expressed
    as a single-table exclusion.

    ``is_locked`` pins this assignment *within this version* — it is an
    optimizer input meaning "LNS may not move this". A durable institutional
    pin ("the Dean froze this class for the year") is a different thing and
    belongs in `PlanningConstraint` as a lock constraint; it must not be
    implemented by copying ``is_locked`` forward between versions.
    """

    __tablename__ = "schedule_assignment"
    __table_args__ = (
        UniqueConstraint(
            "schedule_version_id",
            "class_session_id",
            name="uq_schedule_assignment_schedule_version_id_class_session_id",
        ),
        # No two assignments in the same version may occupy the same room at
        # overlapping times on the same day. Needs btree_gist (created in the
        # first migration).
        ExcludeConstraint(
            ("schedule_version_id", "="),
            ("room_id", "="),
            ("day_of_week", "="),
            (
                text("int4range(start_slot_index, start_slot_index + duration_slots)"),
                "&&",
            ),
            name="ex_schedule_assignment_no_room_double_booking",
            using="gist",
            where=text("room_id IS NOT NULL"),
        ),
        # An in-person session must end up in a room; an asynchronous one
        # must not silently acquire one.
        CheckConstraint(
            "requires_room = (room_id IS NOT NULL)",
            name="room_presence_matches_requirement",
        ),
        CheckConstraint("duration_slots > 0", name="duration_slots_positive"),
        CheckConstraint("start_slot_index >= 0", name="start_slot_index_non_negative"),
        # Bind the denormalised copies to their sources.
        ForeignKeyConstraint(
            ["schedule_version_id", "academic_period_id"],
            ["schedule_version.id", "schedule_version.academic_period_id"],
            name="fk_schedule_assignment_version_period",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["class_session_id", "academic_period_id"],
            ["class_session.id", "class_session.academic_period_id"],
            name="fk_schedule_assignment_session_period",
        ),
        ForeignKeyConstraint(
            ["class_session_id", "requires_room"],
            ["class_session.id", "class_session.requires_room"],
            name="fk_schedule_assignment_session_requires_room",
        ),
        ForeignKeyConstraint(
            ["class_session_id", "duration_slots"],
            ["class_session.id", "class_session.duration_slots"],
            name="fk_schedule_assignment_session_duration",
        ),
        ForeignKeyConstraint(
            ["start_timeslot_id", "academic_period_id"],
            ["timeslot.id", "timeslot.academic_period_id"],
            name="fk_schedule_assignment_timeslot_period",
        ),
        ForeignKeyConstraint(
            ["start_timeslot_id", "day_of_week", "start_slot_index"],
            ["timeslot.id", "timeslot.day_of_week", "timeslot.day_slot_index"],
            name="fk_schedule_assignment_timeslot_position",
        ),
        Index(
            "ix_schedule_assignment_version_timeslot",
            "schedule_version_id",
            "start_timeslot_id",
        ),
        Index(
            "ix_schedule_assignment_version_room",
            "schedule_version_id",
            "room_id",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    schedule_version_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, nullable=False, index=True
    )
    class_session_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, nullable=False, index=True
    )
    academic_period_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False)
    start_timeslot_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False)
    #: Denormalised from the start timeslot; part of the occupancy range.
    day_of_week: Mapped[DayOfWeek] = mapped_column(
        pg_enum(DayOfWeek, "day_of_week"), nullable=False
    )
    #: Denormalised from the start timeslot's `day_slot_index`. In-day, so
    #: occupancy arithmetic can never straddle a day boundary.
    start_slot_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Denormalised from the session, bound by composite FK.
    duration_slots: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    requires_room: Mapped[bool] = mapped_column(Boolean, nullable=False)
    room_id: Mapped[uuid.UUID | None] = fk_column("room.id", nullable=True)
    is_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    schedule_version: Mapped[ScheduleVersion] = relationship(
        back_populates="assignments",
        foreign_keys=[schedule_version_id, academic_period_id],
        viewonly=True,
    )
    class_session: Mapped[ClassSession] = relationship(
        foreign_keys=[class_session_id, academic_period_id], viewonly=True
    )
    start_timeslot: Mapped[Timeslot] = relationship(
        foreign_keys=[start_timeslot_id, academic_period_id], viewonly=True
    )
    room: Mapped[Room | None] = relationship(foreign_keys=[room_id])

    @property
    def end_slot_index_exclusive(self) -> int:
        """Exclusive end of the occupied in-day slot range."""
        return self.start_slot_index + self.duration_slots
