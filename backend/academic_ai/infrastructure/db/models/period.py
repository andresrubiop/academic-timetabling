"""Academic periods and the timeslot grid.

The timeslot grid is the most irreversible constant in the platform: every
availability row, every session duration and every schedule assignment is
denominated in slots. Getting it wrong is not a migration, it is a re-plan.
See `docs/adr/0006-timeslot-grid.md`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ExcludeConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import AcademicPeriodStatus, DayOfWeek
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
    from academic_ai.infrastructure.db.models.organization import Institution


class AcademicPeriod(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A term/semester. Everything schedulable is scoped to a period.

    ``slot_minutes`` fixes the granularity of this period's grid. It lives on
    the period rather than in configuration because institutions change their
    bell schedule between periods, never within one, and because every
    session duration in the period must be a whole multiple of it — a rule
    that is only checkable if the value is stored next to the data it
    governs.
    """

    __tablename__ = "academic_period"
    __table_args__ = (
        UniqueConstraint("institution_id", "code", name="uq_academic_period_institution_id_code"),
        CheckConstraint("end_date > start_date", name="end_after_start"),
        # `mod(...)` rather than the `%` operator: `%` is a bind-parameter
        # marker in SQLAlchemy/Alembic text and gets escaped into `%%`.
        CheckConstraint(
            "slot_minutes > 0 AND mod(1440, slot_minutes) = 0",
            name="slot_minutes_divides_a_day",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    institution_id: Mapped[uuid.UUID] = fk_column("institution.id")
    code: Mapped[str] = code_column(32)
    name: Mapped[str] = name_column()
    start_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    end_date: Mapped[dt.date] = mapped_column(Date, nullable=False)
    #: Length of one grid slot, in minutes. Every session duration in this
    #: period must be a whole multiple of this value.
    slot_minutes: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="60"
    )
    status: Mapped[AcademicPeriodStatus] = mapped_column(
        pg_enum(AcademicPeriodStatus, "academic_period_status"),
        nullable=False,
        default=AcademicPeriodStatus.PLANNING,
    )

    institution: Mapped[Institution] = relationship()
    timeslots: Mapped[list[Timeslot]] = relationship(
        back_populates="academic_period", cascade="all, delete-orphan"
    )


class Timeslot(Base, TimestampMixin):
    """One atomic unit of the weekly scheduling grid.

    Slots are atomic and uniform; a longer session occupies
    ``duration_slots`` **consecutive** slots. This is what lets CP-SAT model
    occupancy with integer intervals over ``day_slot_index`` instead of
    datetime arithmetic, and makes availability a plain per-slot relation.

    Two indices are stored, and the distinction matters:

    * ``day_slot_index`` is dense *within one day*, starting at 0. Session
      occupancy arithmetic uses this one, so a session can never straddle a
      day boundary — the arithmetic simply runs out of slots.
    * ``global_slot_index`` is dense across the whole week and exists for
      ordering and for week-level features (teacher gaps, compactness).

    The grid is **dense**: every slot of the working day exists as a row.
    Breaks are represented by ``is_schedulable = False``, not by omitting
    rows. If breaks were holes, ``day_slot_index + duration_slots - 1``
    would jump the gap and a three-slot class would silently straddle lunch.
    """

    __tablename__ = "timeslot"
    __table_args__ = (
        UniqueConstraint(
            "academic_period_id",
            "day_of_week",
            "start_minute",
            name="uq_timeslot_academic_period_id_day_of_week_start_minute",
        ),
        UniqueConstraint(
            "academic_period_id",
            "day_of_week",
            "day_slot_index",
            name="uq_timeslot_academic_period_id_day_of_week_day_slot_index",
        ),
        UniqueConstraint(
            "academic_period_id",
            "global_slot_index",
            name="uq_timeslot_academic_period_id_global_slot_index",
        ),
        # Two slots of the same period and day may not overlap in time.
        # Requires the btree_gist extension (created in the first migration).
        ExcludeConstraint(
            ("academic_period_id", "="),
            ("day_of_week", "="),
            (text("int4range(start_minute, end_minute)"), "&&"),
            name="ex_timeslot_no_overlap_within_day",
            using="gist",
        ),
        CheckConstraint("end_minute > start_minute", name="end_after_start"),
        CheckConstraint(
            "start_minute >= 0 AND end_minute <= 1440", name="within_one_day"
        ),
        CheckConstraint("day_slot_index >= 0", name="day_slot_index_non_negative"),
        CheckConstraint("global_slot_index >= 0", name="global_slot_index_non_negative"),
        Index(
            "ix_timeslot_period_day_index",
            "academic_period_id",
            "day_of_week",
            "day_slot_index",
        ),
        # Composite targets for child tables that denormalise the period, the
        # day and the in-day index alongside a timeslot reference. Schedule
        # assignments carry all three so room-occupancy exclusion can be a
        # pure index operation, and these keys stop the copies drifting.
        UniqueConstraint(
            "id", "academic_period_id", name="uq_timeslot_id_academic_period_id"
        ),
        UniqueConstraint(
            "id",
            "day_of_week",
            "day_slot_index",
            name="uq_timeslot_id_day_of_week_day_slot_index",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    academic_period_id: Mapped[uuid.UUID] = fk_column(
        "academic_period.id", ondelete="CASCADE"
    )
    day_of_week: Mapped[DayOfWeek] = mapped_column(
        pg_enum(DayOfWeek, "day_of_week"), nullable=False
    )
    #: Minutes since 00:00 local time.
    start_minute: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    end_minute: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Dense ordinal within the day; 0 is the day's first slot.
    day_slot_index: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Dense ordinal across the week; 0 is the first slot of the first day.
    global_slot_index: Mapped[int] = mapped_column(Integer, nullable=False)
    #: False for lunch, assembly hours and any other blocked band. Blocked
    #: slots exist as rows so that occupancy arithmetic stays contiguous.
    is_schedulable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    label: Mapped[str | None] = mapped_column(String(64), nullable=True)

    academic_period: Mapped[AcademicPeriod] = relationship(back_populates="timeslots")

    @property
    def duration_minutes(self) -> int:
        return self.end_minute - self.start_minute
