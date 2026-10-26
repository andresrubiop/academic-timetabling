"""SESSION 30: a FET file becomes a period you can browse.

The reader has understood FET files since the FET work, and produces a
`BaseSnapshot` the optimizer can solve — in memory, writing nothing. `demo.py`
writes rows but invents them. There was no bridge, so a real timetable could
be solved and never stored, browsed or changed.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from academic_ai.bootstrap import create_administrator
from academic_ai.import_fet import build, program_code_of, refuse_if_unsafe
from academic_ai.infrastructure.db.models import (
    ClassSession,
    Institution,
    Program,
    ScheduleAssignment,
    Teacher,
)

FET_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "Ings-PQBA26.fet")
STRONG = "una-contrasena-suficientemente-larga"


class TestProgrammeCodes:
    """Derived from group names, which is a heuristic and is reported as one.

    FET carries no programme structure the reader preserves. This institution
    names groups `1ro-ITI` and joins shared activities with `+` — sometimes
    with *student* names rather than group names, which is why only the first
    token is worth trusting.
    """

    def test_a_level_prefix_is_dropped(self) -> None:
        assert program_code_of("1ro-ITI") == "ITI"
        assert program_code_of("6to-PsC") == "PsC"

    def test_a_lettered_level_is_still_a_level(self) -> None:
        # This file carries `2doA-NI` and `2doB-NI`: two parallel groups of
        # the same second-year programme. Without allowing the letter they
        # survived as two distinct "programmes" — a level and a group
        # masquerading as a degree.
        assert program_code_of("2doA-NI") == "NI"
        assert program_code_of("2doB-NI") == "NI"

    def test_a_parallel_group_suffix_is_dropped(self) -> None:
        # `PsC-A` and `PsC-B` are two groups of one programme, not two
        # programmes.
        assert program_code_of("6to-PsC-A") == "PsC"
        assert program_code_of("6to-PsC-B") == "PsC"

    def test_only_the_first_token_of_a_shared_activity_is_trusted(self) -> None:
        # `ITI+Isaac Castro+Mateo Miño` lists students, not programmes.
        # Treating each as a programme invented forty-four of them.
        assert program_code_of("1ro-ITI+Isaac Castro+Mateo Miño") == "ITI"
        assert program_code_of("2do-IN+2do-NI+3ro-NI") == "IN"

    def test_nothing_usable_yields_nothing(self) -> None:
        # The caller puts these in one catch-all rather than inventing a
        # programme per unparseable label.
        assert program_code_of("") == ""
        assert program_code_of("   ") == ""


@pytest.mark.skipif(
    not os.path.exists(FET_FILE), reason="the FET file is not in this checkout"
)
class TestImportingTheRealFile:
    """Against the actual file, because a synthetic FET fixture would test the
    fixture. It is gitignored — it carries real names — so these skip where it
    is absent rather than failing."""

    def _imported(self, session: Session, **kwargs):
        create_administrator(
            session, email="fet@example.test", full_name="FET", password=STRONG
        )
        return build(session, FET_FILE, code="TEST26", **kwargs)

    def test_the_file_becomes_a_period(self, session: Session) -> None:
        result, _diagnostics = self._imported(session)

        assert result.sessions > 300
        assert result.teachers == 91
        assert result.rooms == 27
        assert result.programs > 1

    def test_teacher_names_are_pseudonymised_by_default(
        self, session: Session
    ) -> None:
        # The one decision in this module with a person on the other end of
        # it. A FET file identifies teachers by their real names; the
        # deployment answers over plain HTTP with no row-level scope on the
        # timetable.
        result, _ = self._imported(session)
        assert result.anonymised

        names = set(session.scalars(select(Teacher.full_name)).all())
        assert names

        # The property that matters, rather than a naming convention: not one
        # name in the database appears in the file. Asserting a `Docente NN`
        # prefix tested the placeholder scheme and would have passed for any
        # replacement, including one that leaked a real name through a path
        # the scheme did not cover.
        from academic_ai.infrastructure.fet.reader import read_fet_file

        real = {t.id for t in read_fet_file(FET_FILE, period_id="X").snapshot.teachers}
        assert names & real == set()
        assert len(names) == len(real), "two teachers collapsed into one name"

    def test_real_names_are_opt_in(self, session: Session) -> None:
        result, _ = self._imported(session, real_names=True)
        assert not result.anonymised

        from academic_ai.infrastructure.fet.reader import read_fet_file

        names = set(session.scalars(select(Teacher.full_name)).all())
        real = {t.id for t in read_fet_file(FET_FILE, period_id="X").snapshot.teachers}
        assert names == real

    def test_the_existing_timetable_is_published(self, session: Session) -> None:
        # The file is an existing timetable — every activity carries a fixed
        # start time. Importing it and leaving nothing placed would throw away
        # the thing that makes it worth importing.
        result, _ = self._imported(session)

        assert result.schedule_version_id is not None
        placed = session.scalar(
            select(func.count()).select_from(ScheduleAssignment)
        )
        assert placed == result.sessions

    def test_the_period_loads_back_as_an_instance(self, session: Session) -> None:
        # The round trip that matters: FET -> database -> the same domain type
        # the solver reads. Anything less means it imported and cannot be used.
        from academic_ai.infrastructure.repositories import load_snapshot

        result, _ = self._imported(session)
        snapshot, _diagnostics = load_snapshot(session, result.period_id)

        assert len(snapshot.sessions) == result.sessions
        assert len(snapshot.base_assignments) == result.sessions
        assert snapshot.teachers
        assert snapshot.slots

    def test_the_diagnostics_survive_the_import(self, session: Session) -> None:
        # What FET does not carry is the honest half of the answer, and it
        # must not be dropped on the way into the database.
        _result, diagnostics = self._imported(session)
        codes = {d.code.value for d in diagnostics}
        assert "IMPORT_COHORT_TREATED_AS_STUDENT" in codes
        assert "IMPORT_CRITICALITY_UNAVAILABLE" in codes

    def test_it_refuses_to_import_the_same_code_twice(
        self, session: Session
    ) -> None:
        self._imported(session)
        with pytest.raises(SystemExit, match="already exists"):
            refuse_if_unsafe(session, "TEST26")


class TestReadableLabels:
    """SESSION 32: what a block on the grid actually says.

    A cell used to read `FET-C0120` over `NegInt_1_Comunicacion Asertiva` in a
    section called `8vo-ITI+Carlos Ulcuango` — a synthetic code, a welded
    string, and a **student's name**.
    """

    def test_a_subject_string_splits_into_a_code_and_a_title(self) -> None:
        from academic_ai.import_fet import split_course_name

        assert split_course_name("PsC_6_Psiquiatría y Psicofarmacología") == (
            "PsC-6",
            "Psiquiatría y Psicofarmacología",
        )

    def test_a_string_that_does_not_follow_the_convention_keeps_its_shape(
        self,
    ) -> None:
        # Forced into `code_level_title` it would lose half of itself. No code
        # is a better answer than a wrong one.
        code, title = split_course_name_or_none("Inglés PSICO - I - II-III")
        assert code == ""
        assert title == "Inglés PSICO - I - II-III"

    def test_a_group_label_loses_the_people_in_it(self) -> None:
        # The privacy half. FET joins groups sharing an activity with `+`, and
        # this file's labels sometimes list students by name — which reached
        # the section code and was rendered into every block of a class they
        # attend, on a deployment answering over plain HTTP.
        from academic_ai.import_fet import clean_group_label

        assert clean_group_label("8vo-ITI+Carlos Ulcuango") == "8vo-ITI"
        assert clean_group_label("1ro-NI+1ro-IE") == "1ro-NI"

    def test_an_empty_label_still_produces_a_section_code(self) -> None:
        from academic_ai.import_fet import clean_group_label

        assert clean_group_label("") == "A"

    def test_pseudonyms_are_stable_across_runs(self) -> None:
        # A timetable whose staff are renamed on every import is one nobody
        # can compare with yesterday's.
        from academic_ai.import_fet import pseudonym

        assert pseudonym(7) == pseudonym(7)
        assert pseudonym(7) != pseudonym(8)

    def test_pseudonyms_read_as_people(self) -> None:
        # `Docente 07` reads as a row id and makes every screen look like a
        # database dump. A timetable is full of people.
        from academic_ai.import_fet import pseudonym

        assert len(pseudonym(3).split()) >= 2


def split_course_name_or_none(raw: str):
    from academic_ai.import_fet import split_course_name

    return split_course_name(raw)


class TestPseudonymsAreDistinct:
    def test_ninety_one_teachers_get_ninety_one_names(self) -> None:
        """The collision the stronger assertion caught.

        `(index * 7) % 28` looked varied and was not — gcd(7, 28) is 7, so
        that surname took four values and the whole name repeated every 28
        teachers. With ninety-one of them, two different people appeared under
        one name on the same timetable.
        """
        from academic_ai.import_fet import pseudonym

        names = [pseudonym(i) for i in range(1, 92)]
        assert len(set(names)) == 91

    def test_it_stays_injective_well_past_that(self) -> None:
        from academic_ai.import_fet import pseudonym

        names = [pseudonym(i) for i in range(0, 2000)]
        assert len(set(names)) == 2000


@pytest.mark.skipif(
    not os.path.exists(FET_FILE), reason="the FET file is not in this checkout"
)
class TestTheFilesOwnTimeRestrictions:
    """SESSION 40: they were read and thrown away.

    `ConstraintTeacherNotAvailableTimes` is parsed by the FET reader into
    `unavailable_slot_ids` — 42 of the 91 teachers in this file declare
    unavailability, 1 121 blocked hours in total — and the importer wrote none
    of them. The timetable that arrived was one the optimizer would happily
    re-plan without any of the constraints its author had entered.
    """

    def _imported(self, session: Session):
        create_administrator(
            session, email="avail@example.test", full_name="A", password=STRONG
        )
        return build(session, FET_FILE, code="AVAIL26")

    def test_the_blocked_hours_are_stored(self, session: Session) -> None:
        from academic_ai.infrastructure.db.models import Availability

        result, _ = self._imported(session)

        assert result.blocked_hours > 1000, (
            "the file's time restrictions were dropped on import"
        )
        assert (
            session.scalar(select(func.count()).select_from(Availability))
            == result.blocked_hours
        )

    def test_they_reach_the_problem_instance(self, session: Session) -> None:
        # The point of storing them: a constraint that does not reach the
        # solver is a note in a table.
        from academic_ai.infrastructure.repositories import load_snapshot

        result, _ = self._imported(session)
        snapshot, _diagnostics = load_snapshot(session, result.period_id)

        with_blocks = [t for t in snapshot.teachers if t.unavailable_slot_ids]
        assert len(with_blocks) > 40
        assert sum(len(t.unavailable_slot_ids) for t in snapshot.teachers) > 1000

    def test_a_teacher_who_declared_nothing_is_still_undeclared(
        self, session: Session
    ) -> None:
        # Absence means available, and "nobody said" stays distinguishable
        # from "said they are free" — which is what the analyser reports on.
        from academic_ai.infrastructure.repositories import load_snapshot

        result, _ = self._imported(session)
        snapshot, _ = load_snapshot(session, result.period_id)

        undeclared = [
            t for t in snapshot.teachers if not t.has_declared_availability
        ]
        assert undeclared, "every teacher declared something; wrong test"
        assert all(not t.unavailable_slot_ids for t in undeclared)


@pytest.mark.skipif(
    not os.path.exists(FET_FILE), reason="the FET file is not in this checkout"
)
class TestRoomRestrictionsSurviveTheImport:
    """SESSION 41: 226 constraints that were being dropped.

    `ConstraintSubjectPreferredRooms` and `ConstraintActivityPreferredRoom(s)`
    restrict **374 of the 399 sessions** in this file. The reader resolved
    them to `allowed_room_ids`; the importer wrote none, so the platform
    believed every class could go in every room.
    """

    def _imported(self, session: Session):
        create_administrator(
            session, email="rooms@example.test", full_name="R", password=STRONG
        )
        return build(session, FET_FILE, code="ROOMS26")

    def test_most_sessions_come_out_restricted(self, session: Session) -> None:
        result, _ = self._imported(session)
        assert result.restricted_sessions > 350, (
            "the file's room restrictions were dropped on import"
        )

    def test_they_reach_the_problem_instance(self, session: Session) -> None:
        from academic_ai.infrastructure.repositories import load_snapshot

        result, _ = self._imported(session)
        snapshot, _ = load_snapshot(session, result.period_id)

        restricted = [s for s in snapshot.sessions if s.allowed_room_ids]
        assert len(restricted) == result.restricted_sessions

    def test_the_restriction_is_narrower_than_every_room(
        self, session: Session
    ) -> None:
        # A "restriction" listing all 27 rooms would be no restriction at all,
        # and would look identical in a count.
        from academic_ai.infrastructure.repositories import load_snapshot

        result, _ = self._imported(session)
        snapshot, _ = load_snapshot(session, result.period_id)

        restricted = [s for s in snapshot.sessions if s.allowed_room_ids]
        assert restricted
        assert min(len(s.allowed_room_ids) for s in restricted) >= 1
        assert min(len(s.allowed_room_ids) for s in restricted) < len(snapshot.rooms)


@pytest.mark.skipif(
    not os.path.exists(FET_FILE), reason="the FET file is not in this checkout"
)
class TestSeatsComeFromTheFile:
    """SESSION 41: an invented capacity that contradicted the source.

    FET records no seat demand, and this importer defaulted every section to
    30. That fabricated number then fought the file: 40 sessions ended up with
    **no candidate room at all**, because a class the file had placed in a
    25-seat room was declared to need 30 seats and `ROOM_CAPACITY` excluded
    the very room it was sitting in.
    """

    def _imported(self, session: Session):
        create_administrator(
            session, email="seats@example.test", full_name="S", password=STRONG
        )
        return build(session, FET_FILE, code="SEATS26")

    def test_every_placed_class_has_somewhere_it_could_go(
        self, session: Session
    ) -> None:
        # The property the invented capacity broke. A class the file has
        # already placed must have at least its own room as a candidate, or
        # the platform is asserting the source timetable is impossible.
        from academic_ai.domain.scheduling.domains import compute_domains
        from academic_ai.infrastructure.repositories import (
            load_constraints,
            load_snapshot,
        )

        result, _ = self._imported(session)
        snapshot, _ = load_snapshot(session, result.period_id)
        domains, _ = compute_domains(
            snapshot, load_constraints(session, result.period_id)
        )

        stranded = [
            spec
            for spec in snapshot.sessions
            if spec.requires_room
            and spec.id in domains
            and not domains[spec.id].room_ids
        ]
        assert stranded == [], (
            f"{len(stranded)} class(es) the file placed have no candidate room"
        )

    def test_a_section_is_no_larger_than_the_room_it_sits_in(
        self, session: Session
    ) -> None:
        from academic_ai.infrastructure.repositories import load_snapshot

        result, _ = self._imported(session)
        snapshot, _ = load_snapshot(session, result.period_id)
        rooms = {r.id: r.capacity for r in snapshot.rooms}
        specs = {s.id: s for s in snapshot.sessions}

        for placed in snapshot.base_assignments:
            if not placed.room_id:
                continue
            spec = specs.get(placed.session_id)
            capacity = rooms.get(placed.room_id)
            if spec is None or not capacity:
                continue
            assert spec.seat_demand <= capacity, (
                f"{spec.id} needs {spec.seat_demand} seats and the file put it "
                f"in a room of {capacity}"
            )
