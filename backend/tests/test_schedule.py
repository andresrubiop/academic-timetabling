"""Schedule versions and assignments.

The three properties tested here are the ones that make a published timetable
mean something:

* exactly one published timetable per institution and period;
* a room is never occupied twice;
* a version beyond DRAFT cannot be quietly amended.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy.exc import DatabaseError, IntegrityError
from sqlalchemy.orm import Session

from academic_ai.domain.enums import (
    DayOfWeek,
    Modality,
    ScheduleScope,
    ScheduleVersionStatus,
    SectionStatus,
    SessionType,
)
from academic_ai.infrastructure.db.models import (
    ClassSession,
    Room,
    Schedule,
    ScheduleAssignment,
    ScheduleVersion,
    Section,
)
from academic_ai.domain.enums import RoomType
from tests.conftest import AcademicWorld


def _assignment(world: AcademicWorld, **overrides: object) -> ScheduleAssignment:
    slot = world.timeslots[0]
    kwargs: dict[str, object] = {
        "schedule_version_id": world.version.id,
        "class_session_id": world.class_session.id,
        "academic_period_id": world.period.id,
        "start_timeslot_id": slot.id,
        "day_of_week": slot.day_of_week,
        "start_slot_index": slot.day_slot_index,
        "duration_slots": world.class_session.duration_slots,
        "requires_room": True,
        "room_id": world.room.id,
    }
    kwargs.update(overrides)
    return ScheduleAssignment(**kwargs)


class TestPublicationScope:
    """One published timetable per institution and period — not per schedule.

    Faculties share rooms, teachers and students, so two independently
    published faculty schedules would double-book them with neither
    violating its own constraints.
    """

    def _publish(self, version: ScheduleVersion, world: AcademicWorld) -> None:
        version.status = ScheduleVersionStatus.PUBLISHED
        version.published_at = dt.datetime.now(dt.timezone.utc)
        version.published_by_user_id = world.admin.id

    def test_a_version_can_be_published(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        self._publish(offered.version, offered)
        session.flush()
        assert offered.version.status is ScheduleVersionStatus.PUBLISHED

    def test_publishing_requires_a_publisher_and_a_timestamp(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        offered.version.status = ScheduleVersionStatus.PUBLISHED
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.flush()

    def test_two_faculty_schedules_cannot_both_be_published_in_one_period(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        self._publish(offered.version, offered)
        session.flush()

        faculty_schedule = Schedule(
            institution_id=offered.institution.id,
            academic_period_id=offered.period.id,
            scope=ScheduleScope.FACULTY,
            scope_faculty_id=offered.faculty.id,
            code="ING",
            name="Horario de Ingeniería",
        )
        session.add(faculty_schedule)
        session.flush()

        rival = ScheduleVersion(
            schedule_id=faculty_schedule.id,
            institution_id=offered.institution.id,
            academic_period_id=offered.period.id,
            version_number=1,
            status=ScheduleVersionStatus.PUBLISHED,
            published_at=dt.datetime.now(dt.timezone.utc),
            published_by_user_id=offered.admin.id,
        )
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(rival)
                session.flush()

    def test_drafts_alongside_a_published_version_are_fine(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """Faculty-level *planning* stays possible; only publication is
        centralised."""
        self._publish(offered.version, offered)
        session.flush()

        faculty_schedule = Schedule(
            institution_id=offered.institution.id,
            academic_period_id=offered.period.id,
            scope=ScheduleScope.FACULTY,
            scope_faculty_id=offered.faculty.id,
            code="ING",
            name="Horario de Ingeniería",
        )
        session.add(faculty_schedule)
        session.flush()

        session.add(
            ScheduleVersion(
                schedule_id=faculty_schedule.id,
                institution_id=offered.institution.id,
                academic_period_id=offered.period.id,
                version_number=1,
                status=ScheduleVersionStatus.DRAFT,
            )
        )
        session.flush()

    def test_schedule_scope_must_match_its_scope_reference(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    Schedule(
                        institution_id=offered.institution.id,
                        academic_period_id=offered.period.id,
                        scope=ScheduleScope.FACULTY,
                        scope_faculty_id=None,  # FACULTY scope needs a faculty
                        code="BROKEN",
                        name="Sin facultad",
                    )
                )
                session.flush()


class TestRoomOccupancy:
    def test_a_room_cannot_be_occupied_twice_in_one_version(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        session.add(_assignment(offered))
        session.flush()

        second_section = Section(
            course_offering_id=offered.offering.id,
            academic_period_id=offered.period.id,
            code="B",
            capacity=30,
            status=SectionStatus.OPEN,
        )
        session.add(second_section)
        session.flush()

        second_session = ClassSession(
            section_id=second_section.id,
            academic_period_id=offered.period.id,
            sequence_number=1,
            session_type=SessionType.LECTURE,
            delivery_mode=Modality.ONSITE,
            duration_slots=2,
        )
        session.add(second_session)
        session.flush()

        # Starts one slot later, so it overlaps the first session's 2 slots.
        overlapping = offered.timeslots[1]
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    _assignment(
                        offered,
                        class_session_id=second_session.id,
                        start_timeslot_id=overlapping.id,
                        start_slot_index=overlapping.day_slot_index,
                    )
                )
                session.flush()

    def test_the_same_room_at_a_non_overlapping_time_is_fine(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        session.add(_assignment(offered))
        session.flush()

        second_section = Section(
            course_offering_id=offered.offering.id,
            academic_period_id=offered.period.id,
            code="B",
            capacity=30,
            status=SectionStatus.OPEN,
        )
        session.add(second_section)
        session.flush()

        second_session = ClassSession(
            section_id=second_section.id,
            academic_period_id=offered.period.id,
            sequence_number=1,
            session_type=SessionType.LECTURE,
            delivery_mode=Modality.ONSITE,
            duration_slots=2,
        )
        session.add(second_session)
        session.flush()

        later = offered.timeslots[2]  # first session occupies slots 0-1
        session.add(
            _assignment(
                offered,
                class_session_id=second_session.id,
                start_timeslot_id=later.id,
                start_slot_index=later.day_slot_index,
            )
        )
        session.flush()
        assert session.query(ScheduleAssignment).count() == 2

    def test_different_rooms_at_the_same_time_are_fine(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        session.add(_assignment(offered))
        session.flush()

        other_room = Room(
            building_id=offered.building.id,
            campus_id=offered.campus.id,
            code="A202",
            name="Aula A202",
            room_type=RoomType.CLASSROOM,
            capacity=30,
        )
        session.add(other_room)
        session.flush()

        second_section = Section(
            course_offering_id=offered.offering.id,
            academic_period_id=offered.period.id,
            code="B",
            capacity=30,
            status=SectionStatus.OPEN,
        )
        session.add(second_section)
        session.flush()

        second_session = ClassSession(
            section_id=second_section.id,
            academic_period_id=offered.period.id,
            sequence_number=1,
            session_type=SessionType.LECTURE,
            delivery_mode=Modality.ONSITE,
            duration_slots=2,
        )
        session.add(second_session)
        session.flush()

        session.add(
            _assignment(
                offered,
                class_session_id=second_session.id,
                room_id=other_room.id,
            )
        )
        session.flush()
        assert session.query(ScheduleAssignment).count() == 2

    def test_an_in_person_session_must_land_in_a_room(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(_assignment(offered, room_id=None))
                session.flush()

    def test_an_asynchronous_session_takes_no_room(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """"Needs no room" and "room not assigned yet" must be
        distinguishable, or publication completeness is undecidable."""
        online = ClassSession(
            section_id=offered.section.id,
            academic_period_id=offered.period.id,
            sequence_number=2,
            session_type=SessionType.LECTURE,
            delivery_mode=Modality.ONLINE,
            duration_slots=1,
            requires_timeslot=True,
            requires_room=False,
        )
        session.add(online)
        session.flush()

        slot = offered.timeslots[3]
        session.add(
            _assignment(
                offered,
                class_session_id=online.id,
                start_timeslot_id=slot.id,
                start_slot_index=slot.day_slot_index,
                duration_slots=1,
                requires_room=False,
                room_id=None,
            )
        )
        session.flush()

    def test_requires_room_must_match_the_session(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        """The assignment cannot claim a session needs no room when it does."""
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(_assignment(offered, requires_room=False, room_id=None))
                session.flush()

    def test_duration_must_match_the_session(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(_assignment(offered, duration_slots=3))
                session.flush()

    def test_one_assignment_per_session_per_version(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        session.add(_assignment(offered))
        session.flush()

        later = offered.timeslots[2]
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(
                    _assignment(
                        offered,
                        start_timeslot_id=later.id,
                        start_slot_index=later.day_slot_index,
                    )
                )
                session.flush()


class TestVersionImmutability:
    def test_assignments_can_be_edited_in_a_draft(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        assignment = _assignment(offered)
        session.add(assignment)
        session.flush()

        assignment.is_locked = True
        session.flush()
        assert assignment.is_locked

    def test_assignments_are_frozen_once_the_version_is_published(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        assignment = _assignment(offered)
        session.add(assignment)
        session.flush()

        offered.version.status = ScheduleVersionStatus.PUBLISHED
        offered.version.published_at = dt.datetime.now(dt.timezone.utc)
        offered.version.published_by_user_id = offered.admin.id
        session.flush()

        assignment.is_locked = True
        with pytest.raises(DatabaseError):
            with session.begin_nested():
                session.flush()

    def test_assignments_cannot_be_added_to_a_published_version(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        offered.version.status = ScheduleVersionStatus.PUBLISHED
        offered.version.published_at = dt.datetime.now(dt.timezone.utc)
        offered.version.published_by_user_id = offered.admin.id
        session.flush()

        with pytest.raises(DatabaseError):
            with session.begin_nested():
                session.add(_assignment(offered))
                session.flush()

    def test_assignments_cannot_be_deleted_from_a_published_version(
        self, session: Session, offered: AcademicWorld
    ) -> None:
        assignment = _assignment(offered)
        session.add(assignment)
        session.flush()

        offered.version.status = ScheduleVersionStatus.PUBLISHED
        offered.version.published_at = dt.datetime.now(dt.timezone.utc)
        offered.version.published_by_user_id = offered.admin.id
        session.flush()

        with pytest.raises(DatabaseError):
            with session.begin_nested():
                session.delete(assignment)
                session.flush()


class TestAPublishedVersionCannotBeDeleted:
    """SESSION 13's architecture checkpoint found this and reproduced it.

    `DELETE FROM schedule_version WHERE status = 'PUBLISHED'` succeeded and
    cascaded away every assignment -- the timetable students had already read,
    removed by one statement with no error. The assignment trigger could not
    stop it: during a cascading delete the version row is already gone, so it
    takes its `v_status IS NULL` branch and steps aside, correctly, for its own
    purpose. Nothing guarded the version itself.
    """

    @staticmethod
    def _version(session: Session, world, *, number: int, published: bool):
        from academic_ai.domain.enums import ScheduleScope, ScheduleVersionStatus
        from academic_ai.infrastructure.db.models import Schedule, ScheduleVersion

        schedule = (
            session.query(Schedule)
            .filter_by(academic_period_id=world.period.id)
            .first()
        )
        if schedule is None:
            schedule = Schedule(
                institution_id=world.institution.id,
                academic_period_id=world.period.id,
                scope=ScheduleScope.INSTITUTION,
                code="SCH-CP",
                name="Checkpoint",
            )
            session.add(schedule)
            session.flush()

        kwargs = {}
        if published:
            kwargs = {
                "status": ScheduleVersionStatus.PUBLISHED,
                "published_at": dt.datetime.now(dt.timezone.utc),
                "published_by_user_id": world.admin.id,
            }
        else:
            kwargs = {"status": ScheduleVersionStatus.DRAFT}

        version = ScheduleVersion(
            schedule_id=schedule.id,
            institution_id=world.institution.id,
            academic_period_id=world.period.id,
            version_number=number,
            **kwargs,
        )
        session.add(version)
        session.flush()
        return version

    def test_deleting_a_published_version_is_refused(
        self, session: Session, world
    ) -> None:
        version = self._version(session, world, number=90, published=True)
        session.delete(version)
        with pytest.raises(IntegrityError):
            session.flush()

    def test_a_draft_may_still_be_deleted(self, session: Session, world) -> None:
        """A draft is working material and was never in force. Forcing an
        operator to archive scratch work leaves every period cluttered, which
        is how a rule like this gets dropped."""
        version = self._version(session, world, number=91, published=False)
        session.delete(version)
        session.flush()  # must not raise
