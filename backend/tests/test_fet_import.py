"""FET import.

Most tests run against a small inline FET document rather than the real file:
`Ings-PQBA26.fet` carries real teacher names and is gitignored, so a suite that
required it would not run for anybody else. The tests that do want the real
file skip cleanly when it is absent, and they are the ones that check the
things only real data exposes — 399 activities, an existing timetable, and 12
constraint kinds of which several are not implemented.

The properties worth defending are about honesty rather than parsing: an
importer that silently drops a constraint kind produces an instance the user
believes is faithful, and the schedule that comes back looks perfectly fine.
"""

from __future__ import annotations

import pathlib
import xml.etree.ElementTree as ET

import pytest

from academic_ai.domain.enums import DayOfWeek
from academic_ai.domain.scheduling.diagnostics import DiagnosticCode
from academic_ai.domain.scheduling.domains import compute_domains
from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
from academic_ai.domain.scheduling.problem import ProblemInstance
from academic_ai.infrastructure.fet import read_fet, read_fet_file
from academic_ai.optimization.validator import Assignment, validate

REAL_FILE = pathlib.Path(__file__).resolve().parents[2] / "Ings-PQBA26.fet"

MINIMAL = """<?xml version="1.0" encoding="UTF-8"?>
<fet version="7.5.1">
  <Institution_Name>Test</Institution_Name>
  <Days_List>
    <Number_of_Days>2</Number_of_Days>
    <Day><Name>Lunes</Name></Day>
    <Day><Name>Martes</Name></Day>
  </Days_List>
  <Hours_List>
    <Number_of_Hours>3</Number_of_Hours>
    <Hour><Name>07:00-08:00</Name></Hour>
    <Hour><Name>08:00-09:00</Name></Hour>
    <Hour><Name>09:00-10:00</Name></Hour>
  </Hours_List>
  <Teachers_List>
    <Teacher><Name>Ada</Name></Teacher>
    <Teacher><Name>Grace</Name></Teacher>
  </Teachers_List>
  <Subjects_List>
    <Subject><Name>Calculo</Name></Subject>
    <Subject><Name>Fisica</Name></Subject>
  </Subjects_List>
  <Rooms_List>
    <Room><Name>A1</Name><Capacity>40</Capacity></Room>
    <Room><Name>BIG</Name><Capacity>30000</Capacity></Room>
  </Rooms_List>
  <Students_List>
    <Year>
      <Name>ITI</Name><Number_of_Students>0</Number_of_Students>
      <Group><Name>1ro-ITI</Name><Number_of_Students>25</Number_of_Students></Group>
    </Year>
  </Students_List>
  <Activities_List>
    <Activity>
      <Teacher>Ada</Teacher><Subject>Calculo</Subject><Students>1ro-ITI</Students>
      <Duration>2</Duration><Id>1</Id><Active>true</Active>
    </Activity>
    <Activity>
      <Teacher>Grace</Teacher><Subject>Fisica</Subject><Students>1ro-ITI</Students>
      <Duration>1</Duration><Id>2</Id><Active>true</Active>
    </Activity>
    <Activity>
      <Teacher>Ada</Teacher><Subject>Calculo</Subject><Students>1ro-ITI</Students>
      <Duration>1</Duration><Id>3</Id><Active>false</Active>
    </Activity>
  </Activities_List>
  <Time_Constraints_List>
    <ConstraintBasicCompulsoryTime><Weight_Percentage>100</Weight_Percentage><Active>true</Active></ConstraintBasicCompulsoryTime>
    <ConstraintTeacherNotAvailableTimes>
      <Weight_Percentage>100</Weight_Percentage><Teacher>Ada</Teacher>
      <Not_Available_Time><Day>Lunes</Day><Hour>07:00-08:00</Hour></Not_Available_Time>
      <Active>true</Active>
    </ConstraintTeacherNotAvailableTimes>
    <ConstraintActivityPreferredStartingTime>
      <Weight_Percentage>100</Weight_Percentage><Activity_Id>1</Activity_Id>
      <Day>Martes</Day><Hour>07:00-08:00</Hour>
      <Permanently_Locked>true</Permanently_Locked><Active>true</Active>
    </ConstraintActivityPreferredStartingTime>
    <ConstraintTeachersMaxHoursDaily>
      <Weight_Percentage>100</Weight_Percentage><Maximum_Hours_Daily>6</Maximum_Hours_Daily><Active>true</Active>
    </ConstraintTeachersMaxHoursDaily>
    <ConstraintMinDaysBetweenActivities>
      <Weight_Percentage>95</Weight_Percentage><Number_of_Activities>2</Number_of_Activities>
      <Activity_Id>1</Activity_Id><Activity_Id>2</Activity_Id><MinDays>1</MinDays><Active>true</Active>
    </ConstraintMinDaysBetweenActivities>
  </Time_Constraints_List>
  <Space_Constraints_List>
    <ConstraintActivityPreferredRoom>
      <Weight_Percentage>100</Weight_Percentage><Activity_Id>1</Activity_Id>
      <Room>A1</Room><Active>true</Active>
    </ConstraintActivityPreferredRoom>
  </Space_Constraints_List>
</fet>
"""


@pytest.fixture()
def minimal():
    return read_fet(ET.fromstring(MINIMAL))


class TestGrid:
    def test_days_map_by_position_not_by_name(self, minimal) -> None:
        """FET names days in the institution's own language. Mapping by
        position is what keeps a Spanish file working."""
        assert minimal.snapshot.period.days == (DayOfWeek.MONDAY, DayOfWeek.TUESDAY)

    def test_slot_length_is_derived_from_the_hour_names(self, minimal) -> None:
        assert minimal.snapshot.period.slot_minutes == 60

    def test_the_grid_is_dense(self, minimal) -> None:
        assert len(minimal.snapshot.slots) == 2 * 3
        monday = minimal.snapshot.slots_by_day(DayOfWeek.MONDAY)
        assert [s.in_day_index for s in monday] == [0, 1, 2]

    def test_start_minutes_come_from_the_hour_label(self, minimal) -> None:
        first = minimal.snapshot.slots_by_day(DayOfWeek.MONDAY)[0]
        assert first.start_minute == 7 * 60


class TestPeople:
    def test_teacher_unavailability_becomes_a_deny_list(self, minimal) -> None:
        ada = next(t for t in minimal.snapshot.teachers if t.id == "Ada")
        assert "Lunes|07:00-08:00" in ada.unavailable_slot_ids
        assert ada.has_declared_availability

    def test_a_teacher_with_no_constraint_is_flagged_as_undeclared(
        self, minimal
    ) -> None:
        """"Nobody recorded anything" and "free all week" are the same empty
        deny-list and completely different facts."""
        grace = next(t for t in minimal.snapshot.teachers if t.id == "Grace")
        assert not grace.unavailable_slot_ids
        assert not grace.has_declared_availability


class TestActivities:
    def test_activities_become_sessions(self, minimal) -> None:
        assert {s.id for s in minimal.snapshot.sessions} == {"1", "2"}

    def test_inactive_activities_are_skipped_and_reported(self, minimal) -> None:
        assert "3" not in {s.id for s in minimal.snapshot.sessions}
        assert DiagnosticCode.IMPORT_ACTIVITY_INACTIVE in {
            d.code for d in minimal.diagnostics
        }

    def test_duration_becomes_duration_slots(self, minimal) -> None:
        assert next(s for s in minimal.snapshot.sessions if s.id == "1").duration_slots == 2

    def test_seat_demand_comes_from_the_students_set_size(self, minimal) -> None:
        assert next(s for s in minimal.snapshot.sessions if s.id == "1").seat_demand == 25

    def test_same_subject_and_set_is_one_section(self, minimal) -> None:
        """Which is what makes SECTION_SESSIONS_NO_OVERLAP apply to them."""
        first = next(s for s in minimal.snapshot.sessions if s.id == "1")
        assert first.section_id == "Calculo|1ro-ITI"


class TestStudentsSetsAreNotStudents:
    def test_each_set_becomes_one_synthetic_student(self, minimal) -> None:
        assert {e.student_id for e in minimal.snapshot.enrollments} == {"1ro-ITI"}

    def test_the_substitution_is_reported(self, minimal) -> None:
        """ADR-0009 keeps cohorts out of the conflict model; this is a
        different thing and must say so out loud."""
        assert DiagnosticCode.IMPORT_COHORT_TREATED_AS_STUDENT in {
            d.code for d in minimal.diagnostics
        }

    def test_missing_criticality_data_is_reported(self, minimal) -> None:
        """FET carries no attempt history, so SESSION 5 cannot rank students
        from it and must not pretend to."""
        assert DiagnosticCode.IMPORT_CRITICALITY_UNAVAILABLE in {
            d.code for d in minimal.diagnostics
        }

    def test_sets_have_no_alternatives_so_clashes_are_unresolvable(
        self, minimal
    ) -> None:
        """Faithful to FET, whose students clashes are compulsory."""
        for enrollment in minimal.snapshot.enrollments:
            assert enrollment.alternative_section_count == 0
            assert enrollment.alternative_offering_count == 0
            assert not enrollment.has_alternative


class TestExistingTimetable:
    def test_a_fixed_starting_time_becomes_a_base_assignment(self, minimal) -> None:
        assert len(minimal.snapshot.base_assignments) == 1
        base = minimal.snapshot.base_assignments[0]
        assert base.session_id == "1"
        assert base.start_slot_id == "Martes|07:00-08:00"
        assert base.room_id == "A1"

    def test_permanently_locked_carries_through(self, minimal) -> None:
        assert minimal.snapshot.base_assignments[0].is_locked

    def test_the_presence_of_a_timetable_is_reported(self, minimal) -> None:
        assert DiagnosticCode.IMPORT_BASE_SCHEDULE_DETECTED in {
            d.code for d in minimal.diagnostics
        }


class TestNothingIsSilentlyDropped:
    def test_an_unmapped_constraint_kind_is_named_and_counted(self, minimal) -> None:
        """The whole point. A dropped constraint produces an instance the user
        believes is faithful and a schedule that looks fine."""
        unmapped = [
            d
            for d in minimal.diagnostics
            if d.code is DiagnosticCode.IMPORT_CONSTRAINT_KIND_UNMAPPED
        ]
        assert [d.subject_id for d in unmapped] == [
            "ConstraintMinDaysBetweenActivities"
        ]
        assert unmapped[0].detail["count"] == 1

    def test_mapped_kinds_produce_no_unmapped_diagnostic(self, minimal) -> None:
        reported = {
            d.subject_id
            for d in minimal.diagnostics
            if d.code is DiagnosticCode.IMPORT_CONSTRAINT_KIND_UNMAPPED
        }
        assert "ConstraintTeacherNotAvailableTimes" not in reported
        assert "ConstraintActivityPreferredStartingTime" not in reported
        assert "ConstraintSubjectPreferredRooms" not in reported
        assert "ConstraintTeachersMaxHoursDaily" not in reported

    def test_an_unlimited_capacity_room_is_reported(self, minimal) -> None:
        """Capacity 30000 is FET's "no limit"; such a room satisfies every
        capacity check, so capacity is effectively unenforced there."""
        assert DiagnosticCode.IMPORT_ROOM_CAPACITY_UNBOUNDED in {
            d.code for d in minimal.diagnostics
        }


class TestRoomRestrictions:
    def test_a_subject_room_list_becomes_an_allowed_room_set(self, minimal) -> None:
        """At weight 100 a FET "preferred room" is a restriction, not a wish:
        the generator will not place the activity anywhere else."""
        first = next(s for s in minimal.snapshot.sessions if s.id == "1")
        assert first.allowed_room_ids == frozenset({"A1"})

    def test_no_restriction_means_unrestricted_not_forbidden(self, minimal) -> None:
        """An empty set that meant "nothing allowed" would make every
        ordinary session unplaceable."""
        second = next(s for s in minimal.snapshot.sessions if s.id == "2")
        assert second.allowed_room_ids == frozenset()
        domains, _ = compute_domains(minimal.snapshot, ())
        assert len(domains["2"].room_ids) == 2

    def test_the_restriction_actually_prunes_the_domain(self, minimal) -> None:
        domains, _ = compute_domains(minimal.snapshot, ())
        assert domains["1"].room_ids == ("A1",)

    def test_a_teacher_daily_limit_is_imported(self, minimal) -> None:
        ada = next(t for t in minimal.snapshot.teachers if t.id == "Ada")
        assert ada.max_daily_minutes == 6 * 60


class TestImportIsSolvable:
    def test_the_imported_instance_builds_and_solves(self, minimal) -> None:
        from academic_ai.optimization import solve, SolverConfig

        instance = ProblemInstance(
            snapshot=minimal.snapshot,
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["MINIMAL_DISRUPTION"],
        )
        _, diagnostics = compute_domains(instance.snapshot, instance.constraints)
        outcome = solve(
            instance, SolverConfig(time_limit_seconds=15.0), diagnostics=diagnostics
        )
        assert outcome.is_solved, outcome.failure_reason
        assert outcome.validation is not None and outcome.validation.is_valid

    def test_a_locked_activity_keeps_its_imported_slot(self, minimal) -> None:
        from academic_ai.optimization import solve, SolverConfig

        instance = ProblemInstance(
            snapshot=minimal.snapshot,
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["MINIMAL_DISRUPTION"],
        )
        _, diagnostics = compute_domains(instance.snapshot, instance.constraints)
        outcome = solve(
            instance, SolverConfig(time_limit_seconds=15.0), diagnostics=diagnostics
        )
        placed = {a.session_id: a for a in outcome.assignments}
        assert placed["1"].start_slot_id == "Martes|07:00-08:00"


@pytest.fixture(scope="module")
def real():
    return read_fet_file(REAL_FILE)


@pytest.mark.skipif(not REAL_FILE.exists(), reason="Ings-PQBA26.fet not present")
class TestRealInstitutionalFile:
    """The things only real data exposes. Skipped when the file is absent —
    it is gitignored because it carries real teacher names."""

    def test_the_whole_file_imports(self, real) -> None:
        counts = real.source_counts
        assert counts["days"] == 5
        assert counts["hours"] == 13
        assert counts["teachers"] == 91
        assert counts["rooms"] == 27
        assert counts["activities"] > 350

    def test_it_carries_an_existing_timetable(self, real) -> None:
        assert real.has_base_schedule
        assert len(real.snapshot.base_assignments) == counts_activities(real)

    def test_every_unmapped_kind_is_reported(self, real) -> None:
        unmapped = {
            d.subject_id
            for d in real.diagnostics
            if d.code is DiagnosticCode.IMPORT_CONSTRAINT_KIND_UNMAPPED
        }
        # A known gap, named so nobody assumes it is enforced.
        assert "ConstraintMinDaysBetweenActivities" in unmapped
        # ...and one that used to be a gap and no longer is.
        assert "ConstraintSubjectPreferredRooms" not in unmapped

    def test_our_hard_constraints_admit_the_real_timetable(self, real) -> None:
        """The strongest correctness signal available: the schedule the
        institution actually runs must not violate our HARD constraints.

        The only violations expected are the 36 activities the file leaves
        without a room — an incompleteness in the source, not a clash.
        """
        instance = ProblemInstance(
            snapshot=real.snapshot,
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["MINIMAL_DISRUPTION"],
        )
        existing = tuple(
            Assignment(b.session_id, b.start_slot_id, b.room_id)
            for b in real.snapshot.base_assignments
        )
        report = validate(instance, existing)
        assert set(report.by_code()) <= {"SESSION_ROOM_PRESENCE"}, {
            k: len(v) for k, v in report.by_code().items()
        }

    def test_no_session_is_unplaceable(self, real) -> None:
        instance = ProblemInstance(
            snapshot=real.snapshot,
            constraints=(),
            objective_profile=OBJECTIVE_PROFILES["MINIMAL_DISRUPTION"],
        )
        _, diagnostics = compute_domains(instance.snapshot, instance.constraints)
        assert not [d for d in diagnostics if d.is_blocking]


def counts_activities(imported) -> int:
    return imported.source_counts["activities"]
