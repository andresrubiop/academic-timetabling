"""Facilities and the timeslot grid."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from academic_ai.domain.enums import DayOfWeek, RoomType
from academic_ai.infrastructure.db.models import (
    Building,
    Campus,
    Room,
    Timeslot,
    TravelTime,
)
from tests.conftest import AcademicWorld


class TestRoomCampusConsistency:
    """A room's denormalised campus cannot disagree with its building's."""

    def test_room_campus_must_match_its_building(
        self, session: Session, world: AcademicWorld
    ) -> None:
        other_campus = Campus(
            institution_id=world.institution.id, code="NORTE", name="Campus Norte"
        )
        session.add(other_campus)
        session.flush()

        # Claiming a campus the building does not belong to is rejected by the
        # composite FK, not by application code.
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    Room(
                        building_id=world.building.id,
                        campus_id=other_campus.id,
                        code="X1",
                        name="Aula equivocada",
                        room_type=RoomType.CLASSROOM,
                        capacity=10,
                    )
                )
                session.flush()

    def test_room_with_matching_campus_is_accepted(
        self, session: Session, world: AcademicWorld
    ) -> None:
        room = Room(
            building_id=world.building.id,
            campus_id=world.campus.id,
            code="A102",
            name="Aula A102",
            room_type=RoomType.COMPUTER_LAB,
            capacity=25,
        )
        session.add(room)
        session.flush()
        assert room.id is not None

    def test_capacity_must_be_positive(
        self, session: Session, world: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    Room(
                        building_id=world.building.id,
                        campus_id=world.campus.id,
                        code="A103",
                        name="Aula vacía",
                        room_type=RoomType.CLASSROOM,
                        capacity=0,
                    )
                )
                session.flush()


class TestTravelTime:
    def test_travel_to_the_same_campus_is_rejected(
        self, session: Session, world: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    TravelTime(
                        origin_campus_id=world.campus.id,
                        destination_campus_id=world.campus.id,
                        minutes=10,
                    )
                )
                session.flush()

    def test_travel_time_is_directional(
        self, session: Session, world: AcademicWorld
    ) -> None:
        north = Campus(
            institution_id=world.institution.id, code="NORTE", name="Campus Norte"
        )
        session.add(north)
        session.flush()

        # Asymmetric travel times are legitimate and must both be storable.
        session.add_all(
            [
                TravelTime(
                    origin_campus_id=world.campus.id,
                    destination_campus_id=north.id,
                    minutes=25,
                ),
                TravelTime(
                    origin_campus_id=north.id,
                    destination_campus_id=world.campus.id,
                    minutes=40,
                ),
            ]
        )
        session.flush()

        rows = session.query(TravelTime).all()
        assert sorted(r.minutes for r in rows) == [25, 40]


class TestTimeslotGrid:
    def test_slot_minutes_must_divide_a_day(
        self, session: Session, world: AcademicWorld
    ) -> None:
        world.period.slot_minutes = 50  # 1440 is not a multiple of 50
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.flush()

    def test_overlapping_slots_on_the_same_day_are_rejected(
        self, session: Session, world: AcademicWorld
    ) -> None:
        # 08:30-09:30 overlaps the existing 08:00-09:00 slot.
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    Timeslot(
                        academic_period_id=world.period.id,
                        day_of_week=DayOfWeek.MONDAY,
                        start_minute=8 * 60 + 30,
                        end_minute=9 * 60 + 30,
                        day_slot_index=99,
                        global_slot_index=99,
                    )
                )
                session.flush()

    def test_same_times_on_a_different_day_are_fine(
        self, session: Session, world: AcademicWorld
    ) -> None:
        session.add(
            Timeslot(
                academic_period_id=world.period.id,
                day_of_week=DayOfWeek.TUESDAY,
                start_minute=8 * 60,
                end_minute=9 * 60,
                day_slot_index=0,
                global_slot_index=100,
            )
        )
        session.flush()

    def test_grid_is_dense_and_breaks_are_blocked_slots_not_holes(
        self, session: Session, world: AcademicWorld
    ) -> None:
        """Lunch is a present-but-unschedulable slot.

        This is what stops a 3-slot session that starts at 11:00 from
        silently jumping the lunch break: the index space has no gaps, so the
        occupied range genuinely covers the blocked slot and can be rejected.
        """
        slots = (
            session.query(Timeslot)
            .filter(Timeslot.day_of_week == DayOfWeek.MONDAY)
            .order_by(Timeslot.day_slot_index)
            .all()
        )
        indices = [s.day_slot_index for s in slots]
        assert indices == list(range(len(slots))), "grid must be dense"

        blocked = [s for s in slots if not s.is_schedulable]
        assert len(blocked) == 1
        assert blocked[0].start_minute == 12 * 60

    def test_end_must_be_after_start(
        self, session: Session, world: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    Timeslot(
                        academic_period_id=world.period.id,
                        day_of_week=DayOfWeek.FRIDAY,
                        start_minute=600,
                        end_minute=600,
                        day_slot_index=0,
                        global_slot_index=200,
                    )
                )
                session.flush()

    def test_duration_minutes_is_derived(self, world: AcademicWorld) -> None:
        assert world.timeslots[0].duration_minutes == 60
