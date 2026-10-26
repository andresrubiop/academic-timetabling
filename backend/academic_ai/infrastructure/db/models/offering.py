"""Offerings, sections, sessions — the schedulable hierarchy.

::

    Course            what is taught (catalog, department-owned)
      CourseOffering  that course opened in one period on one campus
        Section       one group (paralelo / NRC) within the offering
          ClassSession  one weekly meeting block  <-- THE SCHEDULABLE UNIT

Only `ClassSession` is scheduled. `ScheduleAssignment` binds exactly one
ClassSession to a start timeslot and (when it needs one) a room.

Two rules here are load-bearing and easy to get wrong:

* **Teacher assignment is authoritative at session level** (`SessionTeacher`),
  not section level. A section-level default with a session-level override
  would force every consumer — CP-SAT builder, independent validator, LNS
  repair, diff engine, API — to reimplement the same resolution rule, with
  no constraint able to catch a divergence. The failure mode is a
  double-booked human being. See `docs/adr/0008-session-level-teachers.md`.
* **Compatible courses are co-taught, never merged into one offering.**
  Every offering keeps exactly one course, so a student's enrollment always
  names the course their curriculum required. Merging is expressed as a
  `SessionCoTeachingGroup` linking sessions that must share a slot and room.
  See `docs/adr/0004-offering-identity.md`.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import (
    Modality,
    OfferingStatus,
    SectionStatus,
    ServiceRole,
    SessionType,
    TeacherRole,
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
    from academic_ai.infrastructure.db.models.course import Course
    from academic_ai.infrastructure.db.models.facilities import RoomFeature
    from academic_ai.infrastructure.db.models.organization import Program
    from academic_ai.infrastructure.db.models.people import Teacher

#: Teacher roles that occupy the teacher's time and therefore participate in
#: the teacher-overlap HARD constraint. Declared here, once, rather than as a
#: string list inside the optimizer.
TIME_OCCUPYING_TEACHER_ROLES = frozenset({TeacherRole.PRIMARY, TeacherRole.ASSISTANT})


class CourseOffering(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """One course opened for one academic period on one campus.

    Exactly one ``course_id``, always. A merged/compatible offering is *not*
    modelled by pointing an offering at several courses: doing so would make
    a student's enrollment name a course their curriculum never required,
    and would make repeat-attempt counting and "how many alternative
    offerings does this student have" unanswerable.

    ``modality`` is the *envelope* — what delivery modes this offering may
    use, and what students it is open to. The per-session reality lives on
    `ClassSession.delivery_mode`.
    """

    __tablename__ = "course_offering"
    __table_args__ = (
        UniqueConstraint(
            "course_id",
            "academic_period_id",
            "campus_id",
            "modality",
            "variant_code",
            name="uq_course_offering_course_period_campus_modality_variant",
            postgresql_nulls_not_distinct=True,
        ),
        # Composite targets: children denormalise course/period and are bound
        # back to this row so the copies cannot drift.
        UniqueConstraint("id", "course_id", name="uq_course_offering_id_course_id"),
        UniqueConstraint(
            "id", "academic_period_id", name="uq_course_offering_id_academic_period_id"
        ),
        Index(
            "ix_course_offering_academic_period_id_status",
            "academic_period_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    course_id: Mapped[uuid.UUID] = fk_column("course.id")
    academic_period_id: Mapped[uuid.UUID] = fk_column("academic_period.id")
    campus_id: Mapped[uuid.UUID] = fk_column("campus.id")
    modality: Mapped[Modality] = mapped_column(
        pg_enum(Modality, "modality"), nullable=False, default=Modality.ONSITE
    )
    #: Distinguishes two otherwise identical offerings (e.g. a morning and an
    #: evening cohort). NULL for the ordinary single-offering case; the
    #: unique key uses NULLS NOT DISTINCT so NULL still deduplicates.
    variant_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[OfferingStatus] = mapped_column(
        pg_enum(OfferingStatus, "offering_status"),
        nullable=False,
        default=OfferingStatus.PLANNED,
    )

    course: Mapped[Course] = relationship()
    programs_served: Mapped[list[CourseOfferingProgram]] = relationship(
        back_populates="course_offering", cascade="all, delete-orphan"
    )
    sections: Mapped[list[Section]] = relationship(
        back_populates="course_offering", cascade="all, delete-orphan"
    )


class CourseOfferingProgram(Base, TimestampMixin, ActorMixin):
    """Which programmes a concrete offering serves this period, and why.

    This is *operational* sharing. It is deliberately separate from
    `CurriculumCourse`, which is the standing academic plan: a course can be
    planned but not offered, or offered to a programme that takes it as a
    free elective without it appearing in that programme's plan.
    """

    __tablename__ = "course_offering_program"
    __table_args__ = (
        UniqueConstraint(
            "course_offering_id",
            "program_id",
            name="uq_course_offering_program_course_offering_id_program_id",
        ),
        # program_id is already indexed by `fk_column(index=True)` below.
    )

    id: Mapped[uuid.UUID] = pk_column()
    course_offering_id: Mapped[uuid.UUID] = fk_column(
        "course_offering.id", ondelete="CASCADE"
    )
    program_id: Mapped[uuid.UUID] = fk_column("program.id")
    service_role: Mapped[ServiceRole] = mapped_column(
        pg_enum(ServiceRole, "service_role"), nullable=False
    )
    #: Planning estimate used before enrollment data exists.
    expected_students: Mapped[int | None] = mapped_column(Integer, nullable=True)

    course_offering: Mapped[CourseOffering] = relationship(
        back_populates="programs_served"
    )
    program: Mapped[Program] = relationship()


class Section(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """One group within an offering (paralelo / NRC)."""

    __tablename__ = "section"
    __table_args__ = (
        UniqueConstraint(
            "course_offering_id", "code", name="uq_section_course_offering_id_code"
        ),
        CheckConstraint("capacity > 0", name="capacity_positive"),
        # Composite targets for children that denormalise offering/period.
        UniqueConstraint(
            "id", "course_offering_id", name="uq_section_id_course_offering_id"
        ),
        UniqueConstraint(
            "id", "academic_period_id", name="uq_section_id_academic_period_id"
        ),
        ForeignKeyConstraint(
            ["course_offering_id", "academic_period_id"],
            ["course_offering.id", "course_offering.academic_period_id"],
            name="fk_section_offering_period",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    course_offering_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, nullable=False, index=True
    )
    #: Denormalised from the offering and bound by composite FK; lets every
    #: period-scoped query on sections and sessions avoid a join.
    academic_period_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, nullable=False, index=True
    )
    code: Mapped[str] = code_column(32)
    capacity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[SectionStatus] = mapped_column(
        pg_enum(SectionStatus, "section_status"),
        nullable=False,
        default=SectionStatus.PLANNED,
    )

    course_offering: Mapped[CourseOffering] = relationship(back_populates="sections")
    sessions: Mapped[list[ClassSession]] = relationship(
        back_populates="section", cascade="all, delete-orphan"
    )
    teacher_loads: Mapped[list[SectionTeacher]] = relationship(
        back_populates="section", cascade="all, delete-orphan"
    )


class SectionTeacher(Base, TimestampMixin, ActorMixin):
    """Contractual teaching load at section level.

    **Not authoritative for scheduling.** The optimizer and the validator
    read `SessionTeacher` and only `SessionTeacher`. This table exists for
    workload reporting and payroll-style views, where "who owns this
    section" is the meaningful question. Keeping it explicitly
    non-authoritative is why there is no resolution rule anywhere.
    """

    __tablename__ = "section_teacher"
    __table_args__ = (
        UniqueConstraint(
            "section_id", "teacher_id", name="uq_section_teacher_section_id_teacher_id"
        ),
        CheckConstraint(
            "load_percentage > 0 AND load_percentage <= 100",
            name="load_percentage_in_range",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    section_id: Mapped[uuid.UUID] = fk_column("section.id", ondelete="CASCADE")
    teacher_id: Mapped[uuid.UUID] = fk_column("teacher.id")
    role: Mapped[TeacherRole] = mapped_column(
        pg_enum(TeacherRole, "teacher_role"), nullable=False
    )
    load_percentage: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, server_default="100"
    )

    section: Mapped[Section] = relationship(back_populates="teacher_loads")
    teacher: Mapped[Teacher] = relationship()


class SessionCoTeachingGroup(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A set of sessions from *different* offerings that are taught together.

    This is how academic compatibility is realised operationally, and it is
    reversible: merging and unmerging is inserting and deleting link rows.
    No enrollment is ever rewritten, no offering is ever destroyed, and a
    what-if scenario can propose a merge without touching committed data
    (playbook SESSION 11, "merge compatible offerings").

    Members must resolve to the same start timeslot and the same room, and
    the room-capacity constraint sums the demand of all member sections.
    The group must be backed by an APPROVED `AcademicCompatibilityGroup` —
    an AI may propose the compatibility, but only a human approves it.
    """

    __tablename__ = "session_co_teaching_group"
    __table_args__ = (
        UniqueConstraint(
            "academic_period_id", "code", name="uq_session_co_teaching_group_period_code"
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    academic_period_id: Mapped[uuid.UUID] = fk_column("academic_period.id")
    academic_compatibility_group_id: Mapped[uuid.UUID] = fk_column(
        "academic_compatibility_group.id"
    )
    code: Mapped[str] = code_column(48)
    #: Composite target so member sessions can be bound to this group's period.
    sessions: Mapped[list[ClassSession]] = relationship(
        back_populates="co_teaching_group"
    )


class ClassSession(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """One weekly meeting block of a section — **the schedulable unit**.

    Domain name is "Session"; the class is `ClassSession` because
    ``Session`` is SQLAlchemy's unit-of-work object and having both in scope
    is a genuine hazard, not a stylistic one.

    ``requires_timeslot`` and ``requires_room`` are explicit rather than
    inferred from modality, because CP-SAT needs a per-schedulable-unit
    boolean and because "asynchronous, needs no room" must be distinguishable
    from "in-person, room not yet assigned". Without that distinction,
    "is this version complete enough to publish?" is undecidable.

    A session with ``requires_timeslot = False`` generates no decision
    variable and does not block publication.
    """

    __tablename__ = "class_session"
    __table_args__ = (
        UniqueConstraint(
            "section_id", "sequence_number", name="uq_class_session_section_id_sequence_number"
        ),
        CheckConstraint("duration_slots > 0", name="duration_slots_positive"),
        CheckConstraint("sequence_number > 0", name="sequence_number_positive"),
        # An asynchronous session cannot require a room: there is no meeting.
        CheckConstraint(
            "requires_timeslot OR NOT requires_room",
            name="room_requires_timeslot",
        ),
        # Co-teaching only makes sense for sessions that are actually placed.
        CheckConstraint(
            "co_teaching_group_id IS NULL OR requires_timeslot",
            name="co_teaching_requires_timeslot",
        ),
        ForeignKeyConstraint(
            ["section_id", "academic_period_id"],
            ["section.id", "section.academic_period_id"],
            name="fk_class_session_section_period",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "id", "academic_period_id", name="uq_class_session_id_academic_period_id"
        ),
        UniqueConstraint(
            "id", "requires_room", name="uq_class_session_id_requires_room"
        ),
        UniqueConstraint(
            "id", "duration_slots", name="uq_class_session_id_duration_slots"
        ),
        Index(
            "ix_class_session_academic_period_id_requires_timeslot",
            "academic_period_id",
            "requires_timeslot",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    section_id: Mapped[uuid.UUID] = mapped_column(UUIDType, nullable=False, index=True)
    #: Denormalised from section (itself bound to the offering) by composite
    #: FK — the optimizer loads one period's sessions in a single scan.
    academic_period_id: Mapped[uuid.UUID] = mapped_column(
        UUIDType, nullable=False, index=True
    )
    sequence_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    session_type: Mapped[SessionType] = mapped_column(
        pg_enum(SessionType, "session_type"), nullable=False
    )
    delivery_mode: Mapped[Modality] = mapped_column(
        pg_enum(Modality, "modality"), nullable=False, default=Modality.ONSITE
    )
    #: Number of consecutive grid slots this session occupies.
    duration_slots: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    requires_timeslot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    requires_room: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )
    #: Minimum room capacity beyond the section's own size, when the session
    #: needs headroom (lab benches, exam spacing). NULL = use section capacity.
    min_room_capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    co_teaching_group_id: Mapped[uuid.UUID | None] = fk_column(
        "session_co_teaching_group.id", nullable=True, ondelete="SET NULL"
    )

    section: Mapped[Section] = relationship(back_populates="sessions")
    co_teaching_group: Mapped[SessionCoTeachingGroup | None] = relationship(
        back_populates="sessions"
    )
    teachers: Mapped[list[SessionTeacher]] = relationship(
        back_populates="class_session", cascade="all, delete-orphan"
    )
    required_features: Mapped[list[SessionRequiredFeature]] = relationship(
        back_populates="class_session", cascade="all, delete-orphan"
    )


class SessionTeacher(Base, TimestampMixin, ActorMixin):
    """Who teaches a session. **The sole scheduling authority for teachers.**

    Teacher overlap is a direct group-by over
    ``session_teacher ⋈ schedule_assignment`` with no interpretation layer.
    Whether a role occupies the teacher's time is decided by
    `TIME_OCCUPYING_TEACHER_ROLES`, not by a string list inside the
    optimizer, so a SUBSTITUTE cannot silently enter the overlap constraint.
    """

    __tablename__ = "session_teacher"
    __table_args__ = (
        UniqueConstraint(
            "class_session_id",
            "teacher_id",
            name="uq_session_teacher_class_session_id_teacher_id",
        ),
        # teacher_id is already indexed by `fk_column(index=True)` below —
        # the "which sessions does this teacher have?" lookup that the
        # teacher-overlap constraint depends on.
    )

    id: Mapped[uuid.UUID] = pk_column()
    class_session_id: Mapped[uuid.UUID] = fk_column(
        "class_session.id", ondelete="CASCADE"
    )
    teacher_id: Mapped[uuid.UUID] = fk_column("teacher.id")
    role: Mapped[TeacherRole] = mapped_column(
        pg_enum(TeacherRole, "teacher_role"),
        nullable=False,
        default=TeacherRole.PRIMARY,
    )

    class_session: Mapped[ClassSession] = relationship(back_populates="teachers")
    teacher: Mapped[Teacher] = relationship()

    @property
    def occupies_time(self) -> bool:
        """Whether this assignment participates in teacher-overlap."""
        return self.role in TIME_OCCUPYING_TEACHER_ROLES


class SessionRequiredFeature(Base, TimestampMixin):
    """A room capability this session requires, in a minimum quantity.

    "Required feature present in sufficient quantity" is a HARD constraint:
    a chemistry lab without fume hoods is not a schedule, it is a hazard.
    """

    __tablename__ = "session_required_feature"
    __table_args__ = (
        UniqueConstraint(
            "class_session_id",
            "room_feature_id",
            name="uq_session_required_feature_class_session_id_room_feature_id",
        ),
        CheckConstraint("min_quantity > 0", name="min_quantity_positive"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    class_session_id: Mapped[uuid.UUID] = fk_column(
        "class_session.id", ondelete="CASCADE"
    )
    room_feature_id: Mapped[uuid.UUID] = fk_column("room_feature.id")
    min_quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="1"
    )

    class_session: Mapped[ClassSession] = relationship(
        back_populates="required_features"
    )
    feature: Mapped[RoomFeature] = relationship()


class ClassSessionAllowedRoom(Base, TimestampMixin):
    """A room this session may use, when the institution restricts them.

    **Empty means unrestricted**, not "no room is allowed". That is the same
    rule `SessionSpec.allowed_room_ids` states and the reason this is a join
    table rather than a flag: the common case is a class that may go anywhere,
    and a table with no rows for it says exactly that at no cost.

    The gap this closes: the FET reader computes these from
    `ConstraintSubjectPreferredRooms` and `ConstraintActivityPreferredRoom(s)`
    — 226 of them in the file we import, restricting **374 of 399 sessions** —
    and there was nowhere to put them. `compute_domains` therefore offered
    every room for every class, so the grid proposed drops into rooms the
    source file forbids and a re-optimisation would scatter laboratories into
    lecture halls.

    Per session rather than per subject, which is what FET says: the reader
    already resolves a subject rule down to the activities it covers, and the
    solver consumes it per session. Storing it the other way round would mean
    re-deriving on every load and losing any restriction that applies to one
    activity of a subject and not the rest.
    """

    __tablename__ = "class_session_allowed_room"
    __table_args__ = (
        UniqueConstraint(
            "class_session_id",
            "room_id",
            name="uq_class_session_allowed_room_session_room",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    class_session_id: Mapped[uuid.UUID] = fk_column(
        "class_session.id", ondelete="CASCADE"
    )
    room_id: Mapped[uuid.UUID] = fk_column("room.id", ondelete="CASCADE")
