"""SESSION 11: scenarios as overlays, minimal-disruption repair, and the diff.

Three things are being checked, and they are different.

**The overlay is honest.** It applies in order, it never mutates the base, and
it *refuses* a step it cannot carry out. Silently skipping a bad step produces
a scenario that answers a question nobody asked and reports the answer with
the same confidence as a real one.

**The repair is minimal.** The point of a what-if is not the best timetable,
it is the smallest change that absorbs the new constraint. A repair that
resolves the constraint by rearranging the period is a wrong answer that looks
like a right one.

**The diff says who it reached.** Not just what moved — moved sessions are
easy; the students, teachers and programmes behind them are the reason anybody
asked.
"""

from __future__ import annotations

import pytest

from academic_ai.application.whatif import build_scenario_instance, run_scenario
from academic_ai.domain.enums import DayOfWeek, SessionType
from academic_ai.domain.scheduling.diff import diff_schedules
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.domain.scheduling.overlay import (
    PERTURBATION_KINDS,
    OverlayError,
    affected_sessions,
    apply_overlay,
)
from academic_ai.domain.scheduling.problem import (
    BaseAssignment,
    BaseSnapshot,
    Overlay,
    PeriodSpec,
    Perturbation,
    RoomSpec,
    SessionSpec,
    SlotSpec,
    StudentEnrollmentSpec,
    TeacherSpec,
)

_MON = DayOfWeek.MONDAY
_TUE = DayOfWeek.TUESDAY


def _base(
    *,
    session_count: int = 3,
    slots_per_day: int = 6,
    days: tuple = (_MON,),
    teachers: tuple[str, ...] = ("T1", "T2"),
    enrollments: tuple[StudentEnrollmentSpec, ...] = (),
    placed: bool = True,
) -> BaseSnapshot:
    slots = tuple(
        SlotSpec(
            f"{day.value[:3].lower()}{i}", day, i, d * 10 + i, (8 + i) * 60
        )
        for d, day in enumerate(days)
        for i in range(slots_per_day)
    )
    sessions = tuple(
        SessionSpec(
            id=f"S{i}",
            section_id=f"sec{i}",
            offering_id=f"off{i}",
            course_id=f"c{i}",
            campus_id="C1",
            duration_slots=1,
            session_type=SessionType.LECTURE,
            teacher_ids=(teachers[(i - 1) % len(teachers)],),
            seat_demand=10,
        )
        for i in range(1, session_count + 1)
    )
    assignments = (
        tuple(
            BaseAssignment(f"S{i}", f"mon{i - 1}", "R1")
            for i in range(1, session_count + 1)
        )
        if placed
        else ()
    )
    return BaseSnapshot(
        period=PeriodSpec(id="P", slot_minutes=60, days=days),
        slots=slots,
        rooms=(
            RoomSpec("R1", "C1", 40, "CLASSROOM", has_declared_availability=True),
            RoomSpec("R2", "C2", 40, "CLASSROOM", has_declared_availability=True),
        ),
        teachers=tuple(
            TeacherSpec(t, has_declared_availability=True) for t in teachers
        ),
        sessions=sessions,
        enrollments=enrollments,
        base_assignments=assignments,
    )


def _overlay(*perturbations: Perturbation, scenario_id: str = "sc") -> Overlay:
    return Overlay(scenario_id=scenario_id, perturbations=perturbations)


def _p(kind: str, seq: int = 1, **kwargs) -> Perturbation:
    return Perturbation(
        kind=kind,
        sequence_number=seq,
        targets=kwargs.pop("targets", {}),
        payload=kwargs.pop("payload", {}),
    )


def _accepted(base: BaseSnapshot) -> dict[str, PlacedSession]:
    return {
        b.session_id: PlacedSession(b.session_id, b.start_slot_id, b.room_id)
        for b in base.base_assignments
    }


# ---------------------------------------------------------------------------
# The overlay
# ---------------------------------------------------------------------------


class TestOverlayIsIsolated:
    def test_the_base_snapshot_is_never_mutated(self) -> None:
        """ADR-0007: base data stays authoritative. If applying an overlay
        edited it, every later scenario would be built on the previous one's
        answer."""
        base = _base()
        before = (base.teachers, base.sessions, base.rooms, base.base_assignments)
        apply_overlay(
            base,
            _overlay(_p("TEACHER_UNAVAILABLE", targets={"teacher_id": "T1"})),
        )
        assert (base.teachers, base.sessions, base.rooms, base.base_assignments) == before

    def test_every_schema_kind_is_implemented(self) -> None:
        """A kind the database accepts and the domain cannot apply is a
        scenario a coordinator can save and never run."""
        from academic_ai.infrastructure.db.models.scenario import (
            SCENARIO_PERTURBATION_KINDS,
        )

        assert set(SCENARIO_PERTURBATION_KINDS) == set(PERTURBATION_KINDS)

    def test_perturbations_apply_in_sequence_order(self) -> None:
        """"Make Ana unavailable, then move her class" is a different question
        from the same pair reversed."""
        base = _base()
        overlay = _overlay(
            _p("MOVE_SESSION", 2, targets={"session_id": "S1"},
               payload={"start_slot_id": "mon4"}),
            _p("MOVE_SESSION", 1, targets={"session_id": "S1"},
               payload={"start_slot_id": "mon5"}),
        )
        result = apply_overlay(base, overlay)
        placed = {b.session_id: b.start_slot_id for b in result.base_assignments}
        assert placed["S1"] == "mon4", "the later step did not win"

    def test_an_unknown_kind_is_refused_not_skipped(self) -> None:
        with pytest.raises(OverlayError, match="unknown perturbation kind"):
            apply_overlay(_base(), _overlay(_p("TELEPORT_EVERYTHING")))


class TestMoveSession:
    def test_it_pins_the_session(self) -> None:
        result = apply_overlay(
            _base(),
            _overlay(
                _p("MOVE_SESSION", targets={"session_id": "S2"},
                   payload={"start_slot_id": "mon5"})
            ),
        )
        placed = {b.session_id: b.start_slot_id for b in result.base_assignments}
        assert placed["S2"] == "mon5"

    def test_moving_in_time_keeps_the_room(self) -> None:
        result = apply_overlay(
            _base(),
            _overlay(
                _p("MOVE_SESSION", targets={"session_id": "S2"},
                   payload={"start_slot_id": "mon5"})
            ),
        )
        moved = next(b for b in result.base_assignments if b.session_id == "S2")
        assert moved.room_id == "R1"

    def test_an_unknown_session_is_refused_by_name(self) -> None:
        """"Invalid scenario" tells the coordinator who wrote it nothing."""
        with pytest.raises(OverlayError, match="GHOST"):
            apply_overlay(
                _base(),
                _overlay(
                    _p("MOVE_SESSION", targets={"session_id": "GHOST"},
                       payload={"start_slot_id": "mon0"})
                ),
            )

    def test_a_slot_outside_the_grid_is_refused(self) -> None:
        with pytest.raises(OverlayError, match="grid"):
            apply_overlay(
                _base(),
                _overlay(
                    _p("MOVE_SESSION", targets={"session_id": "S1"},
                       payload={"start_slot_id": "fri9"})
                ),
            )


class TestAvailabilityPerturbations:
    def test_a_teacher_can_be_made_unavailable_for_named_slots(self) -> None:
        result = apply_overlay(
            _base(),
            _overlay(
                _p("TEACHER_UNAVAILABLE", targets={"teacher_id": "T1"},
                   payload={"slot_ids": ["mon0", "mon1"]})
            ),
        )
        teacher = next(t for t in result.teachers if t.id == "T1")
        assert teacher.unavailable_slot_ids == frozenset({"mon0", "mon1"})

    def test_a_whole_day_can_be_named(self) -> None:
        """How a coordinator actually says "Ana cannot do Tuesdays"."""
        base = _base(days=(_MON, _TUE))
        result = apply_overlay(
            base,
            _overlay(
                _p("TEACHER_UNAVAILABLE", targets={"teacher_id": "T1"},
                   payload={"days": ["TUESDAY"]})
            ),
        )
        teacher = next(t for t in result.teachers if t.id == "T1")
        assert all(s.startswith("tue") for s in teacher.unavailable_slot_ids)
        assert teacher.unavailable_slot_ids

    def test_naming_no_slots_means_the_whole_period(self) -> None:
        base = _base()
        result = apply_overlay(
            base, _overlay(_p("TEACHER_UNAVAILABLE", targets={"teacher_id": "T1"}))
        )
        teacher = next(t for t in result.teachers if t.id == "T1")
        assert teacher.unavailable_slot_ids == frozenset(s.id for s in base.slots)

    def test_the_scenario_makes_availability_declared(self) -> None:
        """Otherwise the "no availability on record" warning fires for
        somebody the coordinator just told us about."""
        import dataclasses

        base = dataclasses.replace(
            _base(),
            teachers=(
                TeacherSpec("T1", has_declared_availability=False),
                TeacherSpec("T2", has_declared_availability=False),
            ),
        )
        result = apply_overlay(
            base,
            _overlay(
                _p("TEACHER_UNAVAILABLE", targets={"teacher_id": "T1"},
                   payload={"slot_ids": ["mon0"]})
            ),
        )
        assert next(t for t in result.teachers if t.id == "T1").has_declared_availability

    def test_a_room_with_no_slots_named_is_closed_entirely(self) -> None:
        """A refurbishment, not a one-off booking."""
        result = apply_overlay(
            _base(), _overlay(_p("ROOM_UNAVAILABLE", targets={"room_id": "R1"}))
        )
        assert not next(r for r in result.rooms if r.id == "R1").is_active

    def test_a_room_with_slots_named_stays_active(self) -> None:
        result = apply_overlay(
            _base(),
            _overlay(
                _p("ROOM_UNAVAILABLE", targets={"room_id": "R1"},
                   payload={"slot_ids": ["mon0"]})
            ),
        )
        room = next(r for r in result.rooms if r.id == "R1")
        assert room.is_active
        assert "mon0" in room.unavailable_slot_ids

    def test_closing_a_campus_deactivates_its_rooms_only(self) -> None:
        result = apply_overlay(
            _base(),
            _overlay(
                _p("CHANGE_CAMPUS_AVAILABILITY", targets={"campus_id": "C2"},
                   payload={"is_available": False})
            ),
        )
        by_id = {r.id: r for r in result.rooms}
        assert not by_id["R2"].is_active
        assert by_id["R1"].is_active


class TestStructuralPerturbations:
    def test_changing_the_instructor_replaces_the_teachers(self) -> None:
        result = apply_overlay(
            _base(),
            _overlay(
                _p("CHANGE_INSTRUCTOR", targets={"session_id": "S1"},
                   payload={"teacher_ids": ["T2"]})
            ),
        )
        assert next(s for s in result.sessions if s.id == "S1").teacher_ids == ("T2",)

    def test_changing_the_instructor_to_nobody_is_refused(self) -> None:
        """An empty list leaves the class with nobody to teach it, which is a
        different scenario and should be stated as one."""
        with pytest.raises(OverlayError, match="teacher_ids"):
            apply_overlay(
                _base(),
                _overlay(
                    _p("CHANGE_INSTRUCTOR", targets={"session_id": "S1"},
                       payload={"teacher_ids": []})
                ),
            )

    def test_an_unknown_instructor_is_refused(self) -> None:
        with pytest.raises(OverlayError, match="T99"):
            apply_overlay(
                _base(),
                _overlay(
                    _p("CHANGE_INSTRUCTOR", targets={"session_id": "S1"},
                       payload={"teacher_ids": ["T99"]})
                ),
            )

    def test_room_requirements_can_be_changed(self) -> None:
        result = apply_overlay(
            _base(),
            _overlay(
                _p("CHANGE_ROOM_REQUIREMENT", targets={"session_id": "S1"},
                   payload={"seat_demand": 90, "required_features": {"LAB": 1}})
            ),
        )
        session = next(s for s in result.sessions if s.id == "S1")
        assert session.seat_demand == 90
        assert session.required_features == {"LAB": 1}

    def test_a_requirement_change_that_changes_nothing_is_refused(self) -> None:
        """It would still count as a scenario step and make the diff claim a
        question was asked."""
        with pytest.raises(OverlayError, match="changes nothing"):
            apply_overlay(
                _base(),
                _overlay(
                    _p("CHANGE_ROOM_REQUIREMENT", targets={"session_id": "S1"})
                ),
            )

    def test_a_new_section_copies_the_shape_and_stays_unplaced(self) -> None:
        """The optimizer decides where it goes; pinning it would answer a
        narrower question than the one asked."""
        base = _base()
        result = apply_overlay(
            base,
            _overlay(
                _p("NEW_SECTION", targets={"offering_id": "off1"},
                   payload={"section_code": "B"})
            ),
        )
        added = [s for s in result.sessions if s.id not in {x.id for x in base.sessions}]
        assert len(added) == 1
        assert added[0].offering_id == "off1"
        placed = {b.session_id for b in result.base_assignments}
        assert added[0].id not in placed

    def test_applying_a_new_section_twice_is_refused(self) -> None:
        """Applying the same scenario step twice is a bug, not an increment."""
        overlay = _overlay(
            _p("NEW_SECTION", 1, targets={"offering_id": "off1"},
               payload={"section_code": "B"}),
            _p("NEW_SECTION", 2, targets={"offering_id": "off1"},
               payload={"section_code": "B"}),
        )
        with pytest.raises(OverlayError, match="already exists"):
            apply_overlay(_base(), overlay)

    def test_merging_offerings_groups_them_and_sums_the_seats(self) -> None:
        """Merging by deleting one offering's sessions would lose its
        enrolments -- and the students, who still need to be somewhere."""
        result = apply_overlay(
            _base(),
            _overlay(
                _p("MERGE_COMPATIBLE_OFFERINGS", targets={"offering_id": "off1"},
                   payload={"with_offering_id": "off2"})
            ),
        )
        merged = [s for s in result.sessions if s.id in {"S1", "S2"}]
        assert len({s.co_teaching_group_id for s in merged}) == 1
        assert all(s.co_teaching_group_id for s in merged)
        assert all(s.seat_demand == 20 for s in merged)

    def test_merging_an_offering_with_itself_is_refused(self) -> None:
        with pytest.raises(OverlayError, match="with itself"):
            apply_overlay(
                _base(),
                _overlay(
                    _p("MERGE_COMPATIBLE_OFFERINGS", targets={"offering_id": "off1"},
                       payload={"with_offering_id": "off1"})
                ),
            )

    def test_unlocking_frees_a_placed_session(self) -> None:
        result = apply_overlay(
            _base(),
            _overlay(_p("UNLOCK_ASSIGNMENT", targets={"session_id": "S1"})),
        )
        assert "S1" not in {b.session_id for b in result.base_assignments}

    def test_locking_an_unplaced_session_is_refused(self) -> None:
        """Locking a session that was never placed pins it to nothing."""
        with pytest.raises(OverlayError, match="no assignment to lock"):
            apply_overlay(
                _base(placed=False),
                _overlay(_p("LOCK_ASSIGNMENT", targets={"session_id": "S1"})),
            )


class TestAffectedRegion:
    def test_a_moved_session_is_in_the_region(self) -> None:
        region = affected_sessions(
            _base(),
            _overlay(
                _p("MOVE_SESSION", targets={"session_id": "S2"},
                   payload={"start_slot_id": "mon5"})
            ),
        )
        assert region == frozenset({"S2"})

    def test_an_unavailable_teacher_pulls_in_all_their_sessions(self) -> None:
        region = affected_sessions(
            _base(session_count=4, teachers=("T1",)),
            _overlay(_p("TEACHER_UNAVAILABLE", targets={"teacher_id": "T1"})),
        )
        assert region == frozenset({"S1", "S2", "S3", "S4"})

    def test_an_unavailable_room_pulls_in_what_was_placed_there(self) -> None:
        region = affected_sessions(
            _base(), _overlay(_p("ROOM_UNAVAILABLE", targets={"room_id": "R1"}))
        )
        assert region == frozenset({"S1", "S2", "S3"})

    def test_an_untouched_period_has_an_empty_region(self) -> None:
        assert affected_sessions(_base(), Overlay()) == frozenset()


# ---------------------------------------------------------------------------
# ScheduleDiff
# ---------------------------------------------------------------------------


class TestScheduleDiff:
    def test_it_separates_moved_from_unchanged(self) -> None:
        base = _base()
        instance = build_scenario_instance(base, Overlay())
        before = _accepted(base)
        after = dict(before)
        after["S2"] = PlacedSession("S2", "mon5", "R1")

        diff = diff_schedules(instance, before, after)
        assert [m.session_id for m in diff.moved] == ["S2"]
        assert set(diff.unchanged) == {"S1", "S3"}
        assert diff.moved_count == 1 and diff.unchanged_count == 2

    def test_a_room_change_counts_as_moved(self) -> None:
        base = _base()
        instance = build_scenario_instance(base, Overlay())
        before = _accepted(base)
        after = dict(before)
        after["S1"] = PlacedSession("S1", "mon0", "R2")

        diff = diff_schedules(instance, before, after)
        assert diff.moved[0].changed_room
        assert not diff.moved[0].changed_time

    def test_a_newly_placed_session_is_reported_as_new(self) -> None:
        base = _base()
        instance = build_scenario_instance(base, Overlay())
        before = _accepted(base)
        after = dict(before)
        after["S9"] = PlacedSession("S9", "mon5", "R1")

        diff = diff_schedules(instance, before, after)
        new = next(m for m in diff.moved if m.session_id == "S9")
        assert new.is_new
        assert "newly placed" in new.describe()

    def test_a_session_that_vanished_is_reported_as_dropped(self) -> None:
        """A session that vanished is a much louder fact than one that moved."""
        base = _base()
        instance = build_scenario_instance(base, Overlay())
        before = _accepted(base)
        after = {k: v for k, v in before.items() if k != "S3"}

        diff = diff_schedules(instance, before, after)
        dropped = next(m for m in diff.moved if m.session_id == "S3")
        assert dropped.is_dropped
        assert "no longer placed" in dropped.describe()

    def test_an_identical_pair_is_a_noop(self) -> None:
        base = _base()
        instance = build_scenario_instance(base, Overlay())
        diff = diff_schedules(instance, _accepted(base), _accepted(base))
        assert diff.is_noop
        assert diff.summary() == "nothing changed"

    def test_it_reports_who_was_reached_not_only_what_moved(self) -> None:
        """Moved sessions are easy. The students behind them are the reason
        anybody asked."""
        enrollments = (
            StudentEnrollmentSpec("ana", "off2", ("S2",), True,
                                  alternative_section_count=1),
        )
        base = _base(enrollments=enrollments)
        instance = build_scenario_instance(base, Overlay())
        before = _accepted(base)
        after = dict(before)
        after["S2"] = PlacedSession("S2", "mon5", "R1")

        diff = diff_schedules(instance, before, after)
        assert diff.affected.students == 1
        assert diff.affected.teachers >= 1

    def test_the_delta_is_per_tier(self) -> None:
        base = _base()
        instance = build_scenario_instance(base, Overlay())
        diff = diff_schedules(instance, _accepted(base), _accepted(base))
        assert len(diff.score_delta) == len(diff.tier_codes)
        assert len(diff.tier_codes) > 1


# ---------------------------------------------------------------------------
# Minimal disruption
# ---------------------------------------------------------------------------


class TestMinimalDisruption:
    def test_an_absorbable_change_moves_only_what_it_must(self) -> None:
        """The point of the whole session. A repair that resolves the
        constraint by rearranging the period is a wrong answer that looks like
        a right one."""
        base = _base(session_count=3, teachers=("T1",))
        overlay = _overlay(
            _p("TEACHER_UNAVAILABLE", targets={"teacher_id": "T1"},
               payload={"slot_ids": ["mon1"]})
        )
        outcome = run_scenario(
            base, overlay, _accepted(base), time_limit_seconds=15, repair_iterations=4
        )

        assert outcome.is_solved
        assert outcome.is_absorbable
        moved = {m.session_id for m in outcome.diff.moved}
        assert moved == {"S2"}, f"more than the affected session moved: {moved}"
        assert set(outcome.diff.unchanged) == {"S1", "S3"}

    def test_an_unabsorbable_change_says_so_rather_than_inventing_one(self) -> None:
        """"This cannot be absorbed" is the most useful answer a what-if can
        give. It must not be dressed up as a plan."""
        base = _base(session_count=3, slots_per_day=3, teachers=("T1",))
        overlay = _overlay(
            _p("TEACHER_UNAVAILABLE", targets={"teacher_id": "T1"},
               payload={"slot_ids": ["mon0", "mon1"]})
        )
        outcome = run_scenario(
            base, overlay, _accepted(base), time_limit_seconds=15, repair_iterations=0
        )
        assert not outcome.is_solved
        assert not outcome.is_absorbable
        assert outcome.failure_reason

    def test_a_scenario_writes_nothing_to_the_accepted_schedule(self) -> None:
        base = _base(session_count=3, teachers=("T1",))
        accepted = _accepted(base)
        snapshot = {k: (v.start_slot_id, v.room_id) for k, v in accepted.items()}

        run_scenario(
            base,
            _overlay(
                _p("TEACHER_UNAVAILABLE", targets={"teacher_id": "T1"},
                   payload={"slot_ids": ["mon1"]})
            ),
            accepted,
            time_limit_seconds=10,
            repair_iterations=2,
        )
        assert {k: (v.start_slot_id, v.room_id) for k, v in accepted.items()} == snapshot

    def test_a_broken_scenario_is_reported_not_raised(self) -> None:
        """The coordinator who wrote it needs to be told which step is wrong."""
        base = _base()
        outcome = run_scenario(
            base,
            _overlay(_p("MOVE_SESSION", targets={"session_id": "GHOST"},
                        payload={"start_slot_id": "mon0"})),
            _accepted(base),
            time_limit_seconds=5,
        )
        assert not outcome.is_solved
        assert "GHOST" in (outcome.failure_reason or "")

    def test_the_default_profile_prices_movement(self) -> None:
        """Under GREENFIELD the optimizer is indifferent between the accepted
        timetable and a completely different one of equal quality, and a
        what-if would routinely return the latter."""
        instance = build_scenario_instance(_base(), Overlay())
        codes = {tier.code for tier in instance.objective_profile.tiers}
        assert any("INSTABILITY" in code or "STABILITY" in code for code in codes)

    def test_the_affected_region_is_reported(self) -> None:
        base = _base(session_count=3, teachers=("T1",))
        outcome = run_scenario(
            base,
            _overlay(
                _p("TEACHER_UNAVAILABLE", targets={"teacher_id": "T1"},
                   payload={"slot_ids": ["mon1"]})
            ),
            _accepted(base),
            time_limit_seconds=10,
            repair_iterations=2,
        )
        assert outcome.affected_region == frozenset({"S1", "S2", "S3"})
