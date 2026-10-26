"""Reading a real academic period into the domain's `BaseSnapshot`.

The piece five sessions have ended by recording as missing. Everything above
it -- the optimizer, the feasibility analyser, the impact preview, the what-if
overlay, the dashboards -- was written against `BaseSnapshot` and needs no
change now that one can be built from the database.

There is exactly **one** loader, and both the optimizer and the preview
endpoints call it. That is the property worth protecting: a preview computed
against a different instance from the one the solver would use is worse than
no preview, because it is confidently wrong rather than absent.

Four readings that are decisions, not mechanics
===============================================

**Availability is a deny-list.** `availability` rows with status UNAVAILABLE
block a slot; the absence of any row means "nothing recorded", which is *not*
the same as "available all week" even though the optimizer must treat it that
way. `has_declared_availability` carries that distinction so SESSION 6 can
warn about it rather than the solver silently assuming freedom.

**Only time-occupying teachers create a disjunction.** A SUBSTITUTE is
recorded against a session but is not in the room, so counting them would
make one person's schedule conflict with a class they are not attending.
`TIME_OCCUPYING_TEACHER_ROLES` is the authority.

**Hours become minutes at the boundary.** `Teacher.max_weekly_hours` is what a
registrar types; `TeacherSpec.max_weekly_minutes` is what the model reasons
in. Converting here rather than in the domain keeps one unit inside the
solver, which is where an off-by-sixty would be hardest to see.

**A session's alternatives are counted, not enumerated.** `criticality()`
needs "how many ways out does this student have", and the answer is a count of
sibling sections and parallel offerings -- computed once per offering here
rather than per enrolment, because on a real period that is the difference
between one query and forty thousand.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict
from typing import Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.domain.enums import (
    AvailabilityStatus,
    EnrollmentStatus,
    LifecycleStatus,
    OfferingStatus,
    SectionStatus,
)
from academic_ai.domain.scheduling.diagnostics import Diagnostic, DiagnosticCode
from academic_ai.domain.scheduling.problem import (
    BaseAssignment,
    BaseSnapshot,
    CoTeachingGroupSpec,
    PeriodSpec,
    RoomSpec,
    SessionSpec,
    SlotSpec,
    StudentEnrollmentSpec,
    TeacherSpec,
)

__all__ = ["SnapshotLoadError", "load_snapshot"]

log = logging.getLogger("academic_ai.repositories.snapshot")

#: Enrolments that occupy the student's week. A DROPPED enrolment is history
#: and a COMPLETED one is last period's; scheduling around either would
#: constrain a timetable on behalf of somebody who is not in the room.
_ACTIVE_ENROLMENTS = (
    EnrollmentStatus.REQUESTED,
    EnrollmentStatus.ENROLLED,
    EnrollmentStatus.WAITLISTED,
)


class SnapshotLoadError(LookupError):
    """The period cannot be read into a solvable instance."""


def load_snapshot(
    session: Session,
    academic_period_id: uuid.UUID | str,
    *,
    base_schedule_version_id: uuid.UUID | str | None = None,
) -> tuple[BaseSnapshot, tuple[Diagnostic, ...]]:
    """Build a `BaseSnapshot` for one period, plus what was odd about it.

    Diagnostics rather than exceptions for anything the optimizer can work
    around: a period with one unschedulable room is still solvable, and
    refusing to load it would be refusing to answer a question that has an
    answer. Only a period that cannot produce an instance at all raises.
    """
    from academic_ai.infrastructure.db.models import (
        AcademicPeriod,
        Availability,
        ClassSession,
        CourseOffering,
        Enrollment,
        Room,
        RoomFeature,
        RoomFeatureAssignment,
        Section,
        SessionCoTeachingGroup,
        SessionRequiredFeature,
        SessionTeacher,
        Teacher,
        Timeslot,
        TravelTime,
    )
    from academic_ai.infrastructure.db.models.offering import (
        TIME_OCCUPYING_TEACHER_ROLES,
    )

    period_id = _as_uuid(academic_period_id)
    period = session.get(AcademicPeriod, period_id)
    if period is None:
        raise SnapshotLoadError(f"no academic period {academic_period_id}")

    diagnostics: list[Diagnostic] = []

    # --- the grid ----------------------------------------------------------
    timeslots = session.scalars(
        select(Timeslot)
        .where(Timeslot.academic_period_id == period_id)
        .order_by(Timeslot.global_slot_index)
    ).all()
    if not timeslots:
        raise SnapshotLoadError(
            f"period {period.code} has no timeslot grid; nothing can be placed "
            "until one exists"
        )

    slots = tuple(
        SlotSpec(
            id=str(t.id),
            day_of_week=t.day_of_week,
            in_day_index=t.day_slot_index,
            global_index=t.global_slot_index,
            start_minute=t.start_minute,
            is_schedulable=t.is_schedulable,
        )
        for t in timeslots
    )
    # Ordered by first appearance in the grid rather than by the enum, so the
    # occupancy index in `variables.py` matches the order a coordinator sees.
    days: list = []
    for t in timeslots:
        if t.day_of_week not in days:
            days.append(t.day_of_week)

    period_spec = PeriodSpec(
        id=str(period.id), slot_minutes=period.slot_minutes, days=tuple(days)
    )

    # --- availability, as a deny-list --------------------------------------
    slot_ids = {t.id for t in timeslots}
    teacher_blocked: dict[uuid.UUID, set[str]] = defaultdict(set)
    room_blocked: dict[uuid.UUID, set[str]] = defaultdict(set)
    teacher_declared: set[uuid.UUID] = set()
    room_declared: set[uuid.UUID] = set()

    for row in session.scalars(
        select(Availability).where(Availability.timeslot_id.in_(slot_ids))
    ):
        if row.teacher_id is not None:
            teacher_declared.add(row.teacher_id)
            if row.status is AvailabilityStatus.UNAVAILABLE:
                teacher_blocked[row.teacher_id].add(str(row.timeslot_id))
        if row.room_id is not None:
            room_declared.add(row.room_id)
            if row.status is AvailabilityStatus.UNAVAILABLE:
                room_blocked[row.room_id].add(str(row.timeslot_id))

    # --- rooms -------------------------------------------------------------
    features_by_room: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    for room_id, code, quantity in session.execute(
        select(
            RoomFeatureAssignment.room_id, RoomFeature.code,
            RoomFeatureAssignment.quantity,
        ).join(RoomFeature, RoomFeature.id == RoomFeatureAssignment.room_feature_id)
    ):
        features_by_room[room_id][code] = quantity

    rooms = tuple(
        RoomSpec(
            id=str(r.id),
            campus_id=str(r.campus_id),
            capacity=r.capacity,
            room_type=r.room_type.value,
            features=dict(features_by_room.get(r.id, {})),
            is_active=r.status is LifecycleStatus.ACTIVE,
            unavailable_slot_ids=frozenset(room_blocked.get(r.id, ())),
            has_declared_availability=r.id in room_declared,
        )
        for r in session.scalars(select(Room).order_by(Room.code))
    )

    # --- teachers ----------------------------------------------------------
    minutes = period.slot_minutes
    teachers = tuple(
        TeacherSpec(
            id=str(t.id),
            # Hours in the register, minutes in the model. Converted here so
            # one unit reaches the solver.
            max_weekly_minutes=t.max_weekly_hours * 60 if t.max_weekly_hours else None,
            max_daily_minutes=t.max_daily_hours * 60 if t.max_daily_hours else None,
            unavailable_slot_ids=frozenset(teacher_blocked.get(t.id, ())),
            has_declared_availability=t.id in teacher_declared,
        )
        for t in session.scalars(
            select(Teacher).where(Teacher.status == LifecycleStatus.ACTIVE)
        )
    )

    # --- sessions ----------------------------------------------------------
    session_rows = session.execute(
        select(ClassSession, Section, CourseOffering)
        .join(Section, Section.id == ClassSession.section_id)
        .join(CourseOffering, CourseOffering.id == Section.course_offering_id)
        .where(ClassSession.academic_period_id == period_id)
        .where(Section.status != SectionStatus.CANCELLED)
        .where(CourseOffering.status != OfferingStatus.CANCELLED)
        .order_by(ClassSession.sequence_number)
    ).all()

    if not session_rows:
        raise SnapshotLoadError(
            f"period {period.code} has no class sessions to schedule"
        )

    session_ids = [cs.id for cs, _, _ in session_rows]

    teachers_by_session: dict[uuid.UUID, list[str]] = defaultdict(list)
    for row in session.scalars(
        select(SessionTeacher).where(SessionTeacher.class_session_id.in_(session_ids))
    ):
        # A SUBSTITUTE is recorded but is not in the room; counting them would
        # make one person's schedule clash with a class they do not attend.
        if row.role in TIME_OCCUPYING_TEACHER_ROLES:
            teachers_by_session[row.class_session_id].append(str(row.teacher_id))

    required_by_session: dict[uuid.UUID, dict[str, int]] = defaultdict(dict)
    for cs_id, code, quantity in session.execute(
        select(
            SessionRequiredFeature.class_session_id, RoomFeature.code,
            SessionRequiredFeature.min_quantity,
        )
        .join(RoomFeature, RoomFeature.id == SessionRequiredFeature.room_feature_id)
        .where(SessionRequiredFeature.class_session_id.in_(session_ids))
    ):
        required_by_session[cs_id][code] = quantity

    # --- allowed rooms ------------------------------------------------------
    # Empty means unrestricted, which is the common case and why this is a
    # join table: a class that may go anywhere has no rows and costs nothing.
    from academic_ai.infrastructure.db.models import ClassSessionAllowedRoom

    allowed_rooms: dict[uuid.UUID, set[str]] = defaultdict(set)
    for cs_id, room_id in session.execute(
        select(
            ClassSessionAllowedRoom.class_session_id,
            ClassSessionAllowedRoom.room_id,
        ).where(ClassSessionAllowedRoom.class_session_id.in_(session_ids))
    ):
        allowed_rooms[cs_id].add(str(room_id))

    # --- programmes ---------------------------------------------------------
    # Which programmes an offering serves, and their faculties. This is what
    # `analyse_impact` measures a change's reach with, and what `assess_scope`
    # routes the approval on -- so it is domain data rather than a label.
    from academic_ai.infrastructure.db.models import CourseOfferingProgram, Program

    programs_by_offering: dict[uuid.UUID, list[str]] = defaultdict(list)
    faculties_by_offering: dict[uuid.UUID, list[str]] = defaultdict(list)
    offering_ids = {offering.id for _, _, offering in session_rows}
    if offering_ids:
        for offering_id, program_id, faculty_id in session.execute(
            select(
                CourseOfferingProgram.course_offering_id, Program.id, Program.faculty_id
            )
            .join(Program, Program.id == CourseOfferingProgram.program_id)
            .where(CourseOfferingProgram.course_offering_id.in_(offering_ids))
        ):
            programs_by_offering[offering_id].append(str(program_id))
            if str(faculty_id) not in faculties_by_offering[offering_id]:
                faculties_by_offering[offering_id].append(str(faculty_id))

    # Seats come from the section's capacity: it is what the registrar
    # committed to, and counting current enrolments instead would shrink a
    # room requirement every time somebody dropped out mid-planning.
    sessions = tuple(
        SessionSpec(
            id=str(cs.id),
            section_id=str(section.id),
            offering_id=str(offering.id),
            course_id=str(offering.course_id),
            campus_id=str(offering.campus_id),
            duration_slots=cs.duration_slots,
            session_type=cs.session_type,
            delivery_mode=cs.delivery_mode,
            requires_timeslot=cs.requires_timeslot,
            requires_room=cs.requires_room,
            teacher_ids=tuple(teachers_by_session.get(cs.id, ())),
            required_features=dict(required_by_session.get(cs.id, {})),
            seat_demand=cs.min_room_capacity or section.capacity,
            co_teaching_group_id=(
                str(cs.co_teaching_group_id) if cs.co_teaching_group_id else None
            ),
            allowed_room_ids=frozenset(allowed_rooms.get(cs.id, ())),
            program_ids=tuple(programs_by_offering.get(offering.id, ())),
            faculty_ids=tuple(faculties_by_offering.get(offering.id, ())),
        )
        for cs, section, offering in session_rows
    )

    unassigned = [s.id for s in sessions if s.requires_timeslot and not s.teacher_ids]
    if unassigned:
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.SESSION_HAS_NO_TEACHER,
                f"{len(unassigned)} session(s) have nobody committed to teach "
                "them; they will be scheduled anyway",
                subject_kind="class_session",
                detail={"session_ids": unassigned[:20]},
            )
        )

    # --- co-teaching -------------------------------------------------------
    members: dict[uuid.UUID, list[str]] = defaultdict(list)
    for cs, _, _ in session_rows:
        if cs.co_teaching_group_id:
            members[cs.co_teaching_group_id].append(str(cs.id))

    co_teaching_groups = tuple(
        CoTeachingGroupSpec(
            id=str(group.id),
            member_session_ids=tuple(members.get(group.id, ())),
            # The group exists because a compatibility group was approved;
            # the schema binds them, so reaching this point means approved.
            is_approved=True,
        )
        for group in session.scalars(
            select(SessionCoTeachingGroup).where(
                SessionCoTeachingGroup.academic_period_id == period_id
            )
        )
    )

    # --- enrolments --------------------------------------------------------
    sessions_by_section: dict[uuid.UUID, list[str]] = defaultdict(list)
    for cs, section, _ in session_rows:
        sessions_by_section[section.id].append(str(cs.id))

    sessions_by_offering: dict[uuid.UUID, list[str]] = defaultdict(list)
    for cs, _, offering in session_rows:
        sessions_by_offering[offering.id].append(str(cs.id))

    # How many ways out a student has, counted once per offering rather than
    # once per enrolment: on a real period that is one query against forty
    # thousand.
    sections_per_offering: dict[uuid.UUID, int] = defaultdict(int)
    for _, section, offering in session_rows:
        sections_per_offering[offering.id] += 0  # ensure the key exists
    for offering_id, count in session.execute(
        select(Section.course_offering_id, __import__("sqlalchemy").func.count())
        .where(Section.status != SectionStatus.CANCELLED)
        .group_by(Section.course_offering_id)
    ):
        sections_per_offering[offering_id] = int(count)

    offerings_per_course: dict[uuid.UUID, int] = defaultdict(int)
    for course_id, count in session.execute(
        select(CourseOffering.course_id, __import__("sqlalchemy").func.count())
        .where(CourseOffering.academic_period_id == period_id)
        .where(CourseOffering.status != OfferingStatus.CANCELLED)
        .group_by(CourseOffering.course_id)
    ):
        offerings_per_course[course_id] = int(count)

    course_of_offering = {o.id: o.course_id for _, _, o in session_rows}

    enrollments: list[StudentEnrollmentSpec] = []
    unresolved = 0
    for row in session.scalars(
        select(Enrollment)
        .where(Enrollment.academic_period_id == period_id)
        .where(Enrollment.status.in_(_ACTIVE_ENROLMENTS))
    ):
        if row.section_id is not None:
            resolved = tuple(sessions_by_section.get(row.section_id, ()))
        else:
            # Unplaced: only unambiguous when the offering has one section.
            # ADR-0009 forbids guessing, so anything else resolves to nothing
            # and is reported.
            candidates = sessions_by_offering.get(row.course_offering_id, ())
            resolved = (
                tuple(candidates)
                if sections_per_offering.get(row.course_offering_id, 0) == 1
                else ()
            )
        if not resolved:
            unresolved += 1
            continue

        course_id = course_of_offering.get(row.course_offering_id)
        enrollments.append(
            StudentEnrollmentSpec(
                student_id=str(row.student_id),
                offering_id=str(row.course_offering_id),
                session_ids=resolved,
                is_mandatory=row.is_mandatory,
                # The student's own section does not count as an alternative
                # to itself.
                alternative_section_count=max(
                    0, sections_per_offering.get(row.course_offering_id, 1) - 1
                ),
                alternative_offering_count=max(
                    0, offerings_per_course.get(course_id, 1) - 1
                ),
                is_repeat=row.attempt_number > 1,
                attempt_number=row.attempt_number,
            )
        )

    if unresolved:
        diagnostics.append(
            Diagnostic(
                DiagnosticCode.UNPLACED_ENROLLMENT_AMBIGUOUS,
                f"{unresolved} enrolment(s) name an offering with several "
                "sections and no section of their own, so which sessions they "
                "occupy cannot be decided here (ADR-0009)",
                subject_kind="enrollment",
                detail={"count": unresolved},
            )
        )

    # --- travel ------------------------------------------------------------
    travel_minutes = {
        (str(origin), str(destination)): int(mins)
        for origin, destination, mins in session.execute(
            select(
                TravelTime.origin_campus_id,
                TravelTime.destination_campus_id,
                TravelTime.minutes,
            )
        )
    }

    # --- the schedule being repaired ---------------------------------------
    base_assignments = _load_assignments(session, period_id, base_schedule_version_id)

    snapshot = BaseSnapshot(
        period=period_spec,
        slots=slots,
        rooms=rooms,
        teachers=teachers,
        sessions=sessions,
        co_teaching_groups=co_teaching_groups,
        enrollments=tuple(enrollments),
        travel_minutes=travel_minutes,
        base_assignments=base_assignments,
    )
    log.info(
        "snapshot loaded",
        extra={
            "period": period.code,
            "sessions": len(sessions),
            "slots": len(slots),
            "rooms": len(rooms),
            "teachers": len(teachers),
            "enrolments": len(enrollments),
        },
    )
    return snapshot, tuple(diagnostics)


def _load_assignments(
    session: Session,
    period_id: uuid.UUID,
    version_id: uuid.UUID | str | None,
) -> tuple[BaseAssignment, ...]:
    """The placement a re-optimisation starts from.

    Named explicitly, or the published version. Never a draft picked by
    recency: re-optimising against whichever draft happened to be newest would
    make the result depend on who last pressed save.
    """
    from academic_ai.domain.enums import ScheduleVersionStatus
    from academic_ai.infrastructure.db.models import ScheduleAssignment, ScheduleVersion

    if version_id is not None:
        version = session.get(ScheduleVersion, _as_uuid(version_id))
    else:
        version = session.scalars(
            select(ScheduleVersion)
            .where(ScheduleVersion.academic_period_id == period_id)
            .where(ScheduleVersion.status == ScheduleVersionStatus.PUBLISHED)
            .order_by(ScheduleVersion.published_at.desc())
            .limit(1)
        ).first()

    if version is None:
        return ()

    return tuple(
        BaseAssignment(
            session_id=str(a.class_session_id),
            start_slot_id=str(a.start_timeslot_id),
            room_id=str(a.room_id) if a.room_id else None,
            is_locked=a.is_locked,
        )
        for a in session.scalars(
            select(ScheduleAssignment).where(
                ScheduleAssignment.schedule_version_id == version.id
            )
        )
    )


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
