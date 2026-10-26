"""SESSION 40: dividing a class that runs several hours.

`ClassSession` is *the schedulable unit* — "one weekly meeting block". A class
of three hours is **one** session occupying three contiguous slots, not three
things that happen to sit together, so moving one of its hours is not a move:
it is a statement that this is now two meetings.

Making a drag do that silently would be the worst version of it. Somebody
nudging a block to tidy up a Wednesday would divide a three-hour laboratory
into a one-hour and a two-hour class, and nothing on screen would say so.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from academic_ai.application.split import SplitError, split_session
from academic_ai.domain.enums import ScheduleVersionStatus, UserRole
from academic_ai.infrastructure.db.models import (
    AuditLog,
    ClassSession,
    ScheduleAssignment,
    SessionTeacher,
    User,
)
from academic_ai.security.rbac import AuthorizationError, Principal


@pytest.fixture()
def planner(session: Session) -> Principal:
    user = User(
        email=f"plan-{uuid.uuid4().hex[:6]}@example.test",
        full_name="Planificadora",
        primary_role=UserRole.COORDINATOR,  # holds EDIT_DRAFT_SCHEDULE
    )
    session.add(user)
    session.flush()
    return Principal(user_id=str(user.id), role=UserRole.COORDINATOR)


@pytest.fixture()
def three_hours(session: Session, offered):
    offered.class_session.duration_slots = 3
    session.flush()
    return offered


class TestDividing:
    def test_three_hours_become_one_and_two(
        self, session: Session, three_hours, planner
    ) -> None:
        result = split_session(
            session, planner, three_hours.class_session.id, first_part_slots=1
        )

        assert result.original_slots == 1
        assert result.new_slots == 2
        session.refresh(three_hours.class_session)
        assert three_hours.class_session.duration_slots == 1

    def test_the_new_meeting_is_the_same_teaching(
        self, session: Session, three_hours, planner
    ) -> None:
        # Same section, same type, same teachers: it is the same class said to
        # happen twice, and only the duration had to be divided.
        result = split_session(
            session, planner, three_hours.class_session.id, first_part_slots=2
        )
        fresh = session.get(ClassSession, uuid.UUID(result.new_session_id))

        assert fresh.section_id == three_hours.class_session.section_id
        assert fresh.session_type is three_hours.class_session.session_type
        assert fresh.requires_room == three_hours.class_session.requires_room

        teachers = session.scalars(
            select(SessionTeacher.teacher_id).where(
                SessionTeacher.class_session_id == fresh.id
            )
        ).all()
        assert teachers == [three_hours.teacher.id]

    def test_both_parts_are_scheduled_independently_afterwards(
        self, session: Session, three_hours, planner
    ) -> None:
        # The point of the whole operation.
        from academic_ai.infrastructure.repositories import load_snapshot

        split_session(
            session, planner, three_hours.class_session.id, first_part_slots=1
        )
        snapshot, _ = load_snapshot(session, three_hours.period.id)

        durations = sorted(s.duration_slots for s in snapshot.sessions)
        assert durations == [1, 2]

    def test_a_draft_placement_of_the_original_is_dropped(
        self, session: Session, three_hours, planner
    ) -> None:
        # A class that ran three hours has no meaningful position now that it
        # runs one, and leaving the assignment would claim a length the
        # session no longer has — which the composite foreign key refuses.
        slot = three_hours.timeslots[0]
        session.add(
            ScheduleAssignment(
                schedule_version_id=three_hours.version.id,
                class_session_id=three_hours.class_session.id,
                academic_period_id=three_hours.period.id,
                start_timeslot_id=slot.id,
                day_of_week=slot.day_of_week,
                start_slot_index=slot.day_slot_index,
                duration_slots=3,
                requires_room=True,
                room_id=three_hours.room.id,
            )
        )
        session.flush()

        split_session(
            session, planner, three_hours.class_session.id, first_part_slots=1
        )
        assert session.scalar(
            select(func.count()).select_from(ScheduleAssignment)
        ) == 0

    def test_it_is_audited(
        self, session: Session, three_hours, planner
    ) -> None:
        split_session(
            session, planner, three_hours.class_session.id, first_part_slots=1
        )
        entry = session.scalars(
            select(AuditLog).where(AuditLog.entity_type == "class_session")
        ).one()
        assert entry.before["duration_slots"] == 3
        assert entry.after["duration_slots"] == 1


class TestRefusals:
    def test_a_published_class_is_refused_with_the_path_to_take(
        self, session: Session, three_hours, planner
    ) -> None:
        """The schema says this and the message repeats it in words.

        `schedule_assignment` references its session by
        `(id, duration_slots)`, so a duration cannot change while an
        assignment names the old one — and a published version's assignments
        are immutable. Changing the shape of a published timetable is a new
        version of it.
        """
        slot = three_hours.timeslots[0]
        session.add(
            ScheduleAssignment(
                schedule_version_id=three_hours.version.id,
                class_session_id=three_hours.class_session.id,
                academic_period_id=three_hours.period.id,
                start_timeslot_id=slot.id,
                day_of_week=slot.day_of_week,
                start_slot_index=slot.day_slot_index,
                duration_slots=3,
                requires_room=True,
                room_id=three_hours.room.id,
            )
        )
        session.flush()
        three_hours.version.status = ScheduleVersionStatus.PUBLISHED
        three_hours.version.published_at = dt.datetime.now(dt.timezone.utc)
        three_hours.version.published_by_user_id = three_hours.admin.id
        session.flush()

        with pytest.raises(SplitError, match="change request"):
            split_session(
                session, planner, three_hours.class_session.id, first_part_slots=1
            )

    def test_a_one_hour_class_has_nothing_to_divide(
        self, session: Session, offered, planner
    ) -> None:
        offered.class_session.duration_slots = 1
        session.flush()
        with pytest.raises(SplitError, match="nothing to divide"):
            split_session(
                session, planner, offered.class_session.id, first_part_slots=1
            )

    def test_a_split_point_that_leaves_nothing_is_refused(
        self, session: Session, three_hours, planner
    ) -> None:
        for point in (0, 3, 4, -1):
            with pytest.raises(SplitError, match="between 1 and"):
                split_session(
                    session,
                    planner,
                    three_hours.class_session.id,
                    first_part_slots=point,
                )

    def test_a_role_that_cannot_plan_is_refused(
        self, session: Session, three_hours
    ) -> None:
        student = Principal(user_id=str(uuid.uuid4()), role=UserRole.STUDENT)
        with pytest.raises(AuthorizationError):
            split_session(
                session, student, three_hours.class_session.id, first_part_slots=1
            )

    def test_an_unknown_session_is_refused(
        self, session: Session, planner
    ) -> None:
        with pytest.raises(SplitError, match="no class session"):
            split_session(session, planner, uuid.uuid4(), first_part_slots=1)
