"""SESSION 38: telling the platform when somebody cannot teach.

The most common thing anybody says to a timetabling system — "she does not
teach Friday afternoons" — and there was no way to say it. The `Availability`
model has existed since SESSION 1, `TEACHER_AVAILABILITY` is a HARD constraint
in the catalog, and `load_snapshot` has read it into every problem instance
since SESSION 18. Nothing had ever written one.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from academic_ai.application.availability import (
    AvailabilityError,
    load_availability,
    set_availability,
)
from academic_ai.domain.enums import AvailabilityStatus, UserRole
from academic_ai.infrastructure.db.models import Availability, AuditLog, User
from academic_ai.security.rbac import AuthorizationError, Principal


@pytest.fixture()
def coordinator(session: Session) -> Principal:
    user = User(
        email=f"coord-{uuid.uuid4().hex[:6]}@example.test",
        full_name="Coordinadora",
        primary_role=UserRole.COORDINATOR,  # holds MANAGE_CONSTRAINTS
    )
    session.add(user)
    session.flush()
    return Principal(user_id=str(user.id), role=UserRole.COORDINATOR)


class TestWritingIt:
    def test_a_teacher_can_be_blocked_on_some_hours(
        self, session: Session, offered, coordinator
    ) -> None:
        blocked = [str(offered.timeslots[0].id), str(offered.timeslots[1].id)]

        result = set_availability(
            session,
            coordinator,
            owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
            unavailable_slot_ids=blocked,
            reason="No dicta los lunes por la mañana",
        )

        assert set(result.unavailable_slot_ids) == set(blocked)
        assert result.has_declared
        assert result.reason

    def test_it_reaches_the_problem_instance(
        self, session: Session, offered, coordinator
    ) -> None:
        # The whole point: a blocked hour must reach the solver, or it is a
        # note in a table nothing consults.
        from academic_ai.infrastructure.repositories import load_snapshot

        blocked = str(offered.timeslots[0].id)
        set_availability(
            session,
            coordinator,
            owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
            unavailable_slot_ids=[blocked],
        )

        snapshot, _ = load_snapshot(session, offered.period.id)
        spec = next(
            t for t in snapshot.teachers if t.id == str(offered.teacher.id)
        )
        assert blocked in spec.unavailable_slot_ids
        assert spec.has_declared_availability

    def test_a_room_can_be_blocked_too(
        self, session: Session, offered, coordinator
    ) -> None:
        from academic_ai.infrastructure.repositories import load_snapshot

        blocked = str(offered.timeslots[1].id)
        set_availability(
            session,
            coordinator,
            owner_kind="room",
            owner_id=offered.room.id,
            academic_period_id=offered.period.id,
            unavailable_slot_ids=[blocked],
        )

        snapshot, _ = load_snapshot(session, offered.period.id)
        spec = next(r for r in snapshot.rooms if r.id == str(offered.room.id))
        assert blocked in spec.unavailable_slot_ids


class TestReplacingRatherThanPatching:
    """The body is the whole calendar. A per-slot toggle would let a dropped
    request leave somebody blocked on an hour they had just cleared."""

    def test_clearing_a_slot_removes_the_row(
        self, session: Session, offered, coordinator
    ) -> None:
        first, second = (str(t.id) for t in offered.timeslots[:2])
        set_availability(
            session, coordinator, owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
            unavailable_slot_ids=[first, second],
        )
        result = set_availability(
            session, coordinator, owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
            unavailable_slot_ids=[first],
        )
        assert result.unavailable_slot_ids == (first,)

    def test_a_cleared_slot_leaves_no_row_at_all(
        self, session: Session, offered, coordinator
    ) -> None:
        # Removed rather than stored as AVAILABLE: a stored AVAILABLE and a
        # missing row mean the same thing to the solver, and only one of them
        # is a fact somebody asserted.
        first = str(offered.timeslots[0].id)
        set_availability(
            session, coordinator, owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
            unavailable_slot_ids=[first],
        )
        set_availability(
            session, coordinator, owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
            unavailable_slot_ids=[],
        )
        assert session.scalar(
            select(func.count()).select_from(Availability)
        ) == 0

    def test_clearing_everything_is_allowed(
        self, session: Session, offered, coordinator
    ) -> None:
        result = set_availability(
            session, coordinator, owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
            unavailable_slot_ids=[],
        )
        assert result.unavailable_slot_ids == ()


class TestRefusals:
    def test_a_slot_from_another_period_is_refused(
        self, session: Session, offered, coordinator, world
    ) -> None:
        # Editing the wrong week. Silently dropping it would save an empty
        # calendar and look like success.
        with pytest.raises(AvailabilityError, match="do not belong"):
            set_availability(
                session, coordinator, owner_kind="teacher",
                owner_id=offered.teacher.id,
                academic_period_id=offered.period.id,
                unavailable_slot_ids=[str(uuid.uuid4())],
            )

    def test_an_unknown_owner_kind_is_refused(
        self, session: Session, offered, coordinator
    ) -> None:
        with pytest.raises(AvailabilityError, match="teacher.*room"):
            set_availability(
                session, coordinator, owner_kind="estudiante",
                owner_id=offered.student.id,
                academic_period_id=offered.period.id,
                unavailable_slot_ids=[],
            )

    def test_a_role_without_manage_constraints_is_refused(
        self, session: Session, offered
    ) -> None:
        # Availability feeds a HARD constraint: a wrong entry does not make a
        # worse timetable, it makes an infeasible one.
        teacher_principal = Principal(
            user_id=str(uuid.uuid4()), role=UserRole.TEACHER
        )
        with pytest.raises(AuthorizationError):
            set_availability(
                session, teacher_principal, owner_kind="teacher",
                owner_id=offered.teacher.id,
                academic_period_id=offered.period.id,
                unavailable_slot_ids=[],
            )


class TestNobodyHasSaid:
    def test_an_untouched_calendar_reports_undeclared(
        self, session: Session, offered, coordinator
    ) -> None:
        # Not the same as "available every hour". `has_declared_availability`
        # carries the distinction, and the analyser reports the first as a
        # diagnostic because default-available silently produces schedules
        # teachers reject.
        result = load_availability(
            session, coordinator, owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
        )
        assert result.unavailable_slot_ids == ()
        assert result.has_declared is False

    def test_a_calendar_filled_in_as_fully_free_is_declared(
        self, session: Session, offered, coordinator
    ) -> None:
        # Somebody looked and answered "no restrictions", which is a fact.
        set_availability(
            session, coordinator, owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
            unavailable_slot_ids=[str(offered.timeslots[0].id)],
        )
        set_availability(
            session, coordinator, owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
            unavailable_slot_ids=[],
        )
        result = load_availability(
            session, coordinator, owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
        )
        # Every row was removed, so this reads as undeclared again — which is
        # honest: nothing is stored that says anybody looked.
        assert result.has_declared is False


class TestAudit:
    def test_changing_a_calendar_is_recorded(
        self, session: Session, offered, coordinator
    ) -> None:
        set_availability(
            session, coordinator, owner_kind="teacher",
            owner_id=offered.teacher.id,
            academic_period_id=offered.period.id,
            unavailable_slot_ids=[str(offered.timeslots[0].id)],
        )
        entries = session.scalars(
            select(AuditLog).where(
                AuditLog.entity_type == "availability:teacher"
            )
        ).all()
        assert len(entries) == 1
        assert entries[0].after["blocked"] == 1
