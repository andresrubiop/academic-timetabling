"""Availability and preferences.

These are two different things and the distinction is enforced, not merely
documented:

* `Availability` answers "*can* this resource be here?" It is binary and
  feeds HARD constraints.
* `Preference` answers "*would* this actor rather be here?" It carries a
  weight and feeds SOFT objectives only.

A preference must never be silently promoted into a hard constraint — that
is how "the professor prefers mornings" becomes "the schedule is infeasible".
Anything that genuinely must be enforced is a `PlanningConstraint`, created
by a human with the authority to impose it.

Polymorphic owners use nullable foreign keys plus a ``num_nonnulls`` CHECK
rather than an ``(owner_type, owner_id)`` pair, because the owner set is
small and known, and this way the database still enforces that the owner
exists.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import AvailabilityStatus, DayOfWeek, PreferenceKind
from academic_ai.infrastructure.db.base import (
    ActorMixin,
    Base,
    MetadataMixin,
    TimestampMixin,
    fk_column,
    pg_enum,
    pk_column,
)

if TYPE_CHECKING:
    from academic_ai.infrastructure.db.models.facilities import Campus, Room
    from academic_ai.infrastructure.db.models.people import Teacher
    from academic_ai.infrastructure.db.models.period import Timeslot


class Availability(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """Whether a teacher or a room can be used in a given timeslot.

    Absence of a row means *available*: recording a row for every
    teacher × every slot would be the dominant table in the database for no
    benefit. Only exceptions are stored. The optimizer therefore reads this
    table as a deny-list, and `AvailabilityStatus.AVAILABLE` exists solely to
    carve an exception out of a broader UNAVAILABLE block (for example, "not
    Fridays, except this one slot").
    """

    __tablename__ = "availability"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(teacher_id, room_id) = 1", name="exactly_one_owner"
        ),
        UniqueConstraint(
            "teacher_id",
            "room_id",
            "timeslot_id",
            name="uq_availability_teacher_id_room_id_timeslot_id",
        ),
        Index("ix_availability_timeslot_id_status", "timeslot_id", "status"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    teacher_id: Mapped[uuid.UUID | None] = fk_column(
        "teacher.id", nullable=True, ondelete="CASCADE"
    )
    room_id: Mapped[uuid.UUID | None] = fk_column(
        "room.id", nullable=True, ondelete="CASCADE"
    )
    timeslot_id: Mapped[uuid.UUID] = fk_column("timeslot.id", ondelete="CASCADE")
    status: Mapped[AvailabilityStatus] = mapped_column(
        pg_enum(AvailabilityStatus, "availability_status"), nullable=False
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    teacher: Mapped[Teacher | None] = relationship()
    room: Mapped[Room | None] = relationship()
    timeslot: Mapped[Timeslot] = relationship()


class Preference(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A soft desire, with a weight, owned by a teacher, a programme or a
    student cohort.

    ``weight`` is a relative, unitless strength within its own preference
    kind. SESSION 2 decides how weights combine into the objective; it must
    not assume they are comparable across kinds, and it must not treat any
    weight, however large, as a hard constraint.
    """

    __tablename__ = "preference"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(teacher_id, program_id, student_cohort_id) = 1",
            name="exactly_one_owner",
        ),
        CheckConstraint("weight >= 0", name="weight_non_negative"),
        # Each preference kind uses a different subset of the target columns;
        # this keeps a row from claiming a target its kind cannot use.
        CheckConstraint(
            "(kind IN ('PREFERRED_TIMESLOT', 'AVOID_TIMESLOT')) = (timeslot_id IS NOT NULL)",
            name="timeslot_target_matches_kind",
        ),
        CheckConstraint(
            "(kind IN ('PREFERRED_DAY', 'AVOID_DAY')) = (day_of_week IS NOT NULL)",
            name="day_target_matches_kind",
        ),
        CheckConstraint(
            "(kind = 'PREFERRED_ROOM') = (room_id IS NOT NULL)",
            name="room_target_matches_kind",
        ),
        CheckConstraint(
            "(kind = 'PREFERRED_CAMPUS') = (campus_id IS NOT NULL)",
            name="campus_target_matches_kind",
        ),
        CheckConstraint(
            "(kind = 'MAX_DAILY_HOURS') = (numeric_value IS NOT NULL)",
            name="numeric_target_matches_kind",
        ),
        Index("ix_preference_academic_period_id_kind", "academic_period_id", "kind"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    academic_period_id: Mapped[uuid.UUID | None] = fk_column(
        "academic_period.id", nullable=True, ondelete="CASCADE"
    )

    # --- owner (exactly one) ---
    teacher_id: Mapped[uuid.UUID | None] = fk_column(
        "teacher.id", nullable=True, ondelete="CASCADE"
    )
    program_id: Mapped[uuid.UUID | None] = fk_column(
        "program.id", nullable=True, ondelete="CASCADE"
    )
    student_cohort_id: Mapped[uuid.UUID | None] = fk_column(
        "student_cohort.id", nullable=True, ondelete="CASCADE"
    )

    kind: Mapped[PreferenceKind] = mapped_column(
        pg_enum(PreferenceKind, "preference_kind"), nullable=False
    )

    # --- target (which one is used depends on `kind`) ---
    timeslot_id: Mapped[uuid.UUID | None] = fk_column(
        "timeslot.id", nullable=True, ondelete="CASCADE"
    )
    room_id: Mapped[uuid.UUID | None] = fk_column(
        "room.id", nullable=True, ondelete="CASCADE"
    )
    campus_id: Mapped[uuid.UUID | None] = fk_column(
        "campus.id", nullable=True, ondelete="CASCADE"
    )
    day_of_week: Mapped[DayOfWeek | None] = mapped_column(
        pg_enum(DayOfWeek, "day_of_week"), nullable=True
    )
    numeric_value: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)

    weight: Mapped[float] = mapped_column(
        Numeric(6, 3), nullable=False, server_default="1.0"
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)

    teacher: Mapped[Teacher | None] = relationship()
    timeslot: Mapped[Timeslot | None] = relationship()
    room: Mapped[Room | None] = relationship()
    campus: Mapped[Campus | None] = relationship()
