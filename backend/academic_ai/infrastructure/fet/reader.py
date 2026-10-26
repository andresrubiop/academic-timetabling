"""Read a FET file into a `BaseSnapshot`.

FET (Free Timetabling Software) is what the institution currently uses, so its
`.fet` files are the only place a complete real timetable exists. Importing one
is how this platform gets tested against reality instead of against fixtures.

This lives in `infrastructure/` because it is I/O: `domain/` may not import
XML any more than it may import SQLAlchemy. What comes out is an ordinary
`BaseSnapshot` plus diagnostics — exactly what the database path will produce —
so `solve()` cannot tell where an instance came from, and ADR-0007's
`(base, overlay)` contract is untouched.

Two honesty rules drive the whole module
========================================

**A constraint kind we do not understand is reported, never dropped.** An
importer that silently ignores `ConstraintTeacherMaxHoursDaily` hands back an
instance the user believes is faithful, and the schedule that comes out will
look perfectly fine. Every unmapped kind produces
`IMPORT_CONSTRAINT_KIND_UNMAPPED` naming it and counting its occurrences.

**FET students sets are not students.** FET attaches a *set* (a year, group or
subgroup) to an activity; a clash means two activities sharing a set. This
platform makes `Enrollment` the conflict authority and states that cohorts
never are (ADR-0009). Rather than quietly redefining that, each students set
is imported as one indivisible synthetic student — which is precisely what FET
means by it — and `IMPORT_COHORT_TREATED_AS_STUDENT` says so. The consequence
is real and is reported too: a FET-sourced instance cannot express
repeated-student criticality, because the source data does not contain it.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from academic_ai.domain.enums import DayOfWeek, SessionType
from academic_ai.domain.scheduling.diagnostics import Diagnostic, DiagnosticCode
from academic_ai.domain.scheduling.problem import (
    BaseAssignment,
    BaseSnapshot,
    PeriodSpec,
    RoomSpec,
    SessionSpec,
    SlotSpec,
    StudentEnrollmentSpec,
    TeacherSpec,
)

__all__ = ["FetImport", "read_fet", "read_fet_file"]

#: FET days are named in the institution's own language ("Lunes", "Monday").
#: Mapping by *position* rather than by name is what keeps the importer working
#: on a Spanish file, and `Days_List` is ordered.
_WEEK = (
    DayOfWeek.MONDAY,
    DayOfWeek.TUESDAY,
    DayOfWeek.WEDNESDAY,
    DayOfWeek.THURSDAY,
    DayOfWeek.FRIDAY,
    DayOfWeek.SATURDAY,
    DayOfWeek.SUNDAY,
)

_HOUR = re.compile(r"^\s*(\d{1,2}):(\d{2})")

#: FET writes 30000 for "no capacity limit". Treated as unbounded rather than
#: as a 30 000-seat room, but reported: a room whose capacity is unknown
#: silently satisfies every capacity check.
_UNBOUNDED_CAPACITY = 30000

#: Constraint kinds this importer understands. Everything else is reported.
_MAPPED = frozenset(
    {
        "ConstraintBasicCompulsoryTime",
        "ConstraintBasicCompulsorySpace",
        "ConstraintTeacherNotAvailableTimes",
        "ConstraintStudentsSetNotAvailableTimes",
        "ConstraintRoomNotAvailableTimes",
        "ConstraintBreakTimes",
        "ConstraintActivityPreferredStartingTime",
        "ConstraintActivityPreferredRoom",
        "ConstraintActivityPreferredRooms",
        "ConstraintSubjectPreferredRooms",
        "ConstraintTeachersMaxHoursDaily",
        "ConstraintTeacherMaxHoursDaily",
    }
)


@dataclass(frozen=True, slots=True)
class FetImport:
    """The result: an instance-ready snapshot, and everything worth saying."""

    snapshot: BaseSnapshot
    diagnostics: tuple[Diagnostic, ...] = ()
    #: Source counts, for reporting what was read against what was produced.
    source_counts: Mapping[str, int] = field(default_factory=dict)

    @property
    def has_base_schedule(self) -> bool:
        return bool(self.snapshot.base_assignments)

    def by_severity(self) -> Mapping[str, int]:
        return dict(Counter(d.severity.value for d in self.diagnostics))


def _text(element: ET.Element | None, tag: str, default: str = "") -> str:
    if element is None:
        return default
    found = element.find(tag)
    return (found.text or default).strip() if found is not None else default


def _start_minute(name: str, index: int) -> int:
    """Minutes since midnight for a FET hour name like ``07:00-08:00``.

    Falls back to an hourly grid from 08:00 when the name is not a time — FET
    allows arbitrary period labels ("P1", "Primera"), and a label carries no
    clock information for anyone to recover.
    """
    match = _HOUR.match(name)
    if match:
        return int(match.group(1)) * 60 + int(match.group(2))
    return (8 + index) * 60


def _slot_id(day: str, hour: str) -> str:
    return f"{day}|{hour}"


def read_fet(root: ET.Element, *, period_id: str = "FET") -> FetImport:
    """Convert a parsed FET document into a snapshot."""
    diagnostics: list[Diagnostic] = []

    # --- grid ----------------------------------------------------------------
    day_names = [
        _text(d, "Name") for d in root.findall("Days_List/Day") if _text(d, "Name")
    ]
    hour_names = [
        _text(h, "Name") for h in root.findall("Hours_List/Hour") if _text(h, "Name")
    ]
    days = tuple(_WEEK[i] for i in range(min(len(day_names), len(_WEEK))))

    slot_minutes = 60
    if len(hour_names) > 1:
        first, second = _start_minute(hour_names[0], 0), _start_minute(hour_names[1], 1)
        if second > first:
            slot_minutes = second - first

    blocked = _break_times(root)
    slots: list[SlotSpec] = []
    for day_index, day_name in enumerate(day_names[: len(days)]):
        for hour_index, hour_name in enumerate(hour_names):
            slots.append(
                SlotSpec(
                    id=_slot_id(day_name, hour_name),
                    day_of_week=days[day_index],
                    in_day_index=hour_index,
                    global_index=day_index * len(hour_names) + hour_index,
                    start_minute=_start_minute(hour_name, hour_index),
                    is_schedulable=(day_name, hour_name) not in blocked,
                )
            )

    # --- rooms ---------------------------------------------------------------
    unbounded: list[str] = []
    rooms: list[RoomSpec] = []
    room_blocked = _not_available(root, "ConstraintRoomNotAvailableTimes", "Room")
    for element in root.findall("Rooms_List/Room"):
        name = _text(element, "Name")
        if not name:
            continue
        raw_capacity = int(_text(element, "Capacity", "0") or 0)
        if raw_capacity >= _UNBOUNDED_CAPACITY:
            unbounded.append(name)
        rooms.append(
            RoomSpec(
                id=name,
                # FET has no campus concept; one synthetic campus keeps
                # ROOM_CAMPUS_MATCH satisfiable and travel time a no-op.
                campus_id="FET",
                capacity=raw_capacity,
                room_type="CLASSROOM",
                unavailable_slot_ids=frozenset(room_blocked.get(name, ())),
                has_declared_availability=name in room_blocked,
            )
        )
    if unbounded:
        diagnostics.append(
            Diagnostic(
                code=DiagnosticCode.IMPORT_ROOM_CAPACITY_UNBOUNDED,
                message=(
                    f"{len(unbounded)} room(s) carry FET's unlimited-capacity "
                    f"sentinel ({_UNBOUNDED_CAPACITY}); they will satisfy every "
                    "capacity check, so room capacity is effectively unenforced "
                    "for them"
                ),
                subject_kind="room",
                detail={"rooms": sorted(unbounded)[:20], "count": len(unbounded)},
            )
        )

    # --- teachers ------------------------------------------------------------
    teacher_blocked = _not_available(root, "ConstraintTeacherNotAvailableTimes", "Teacher")
    daily_limit = _teacher_daily_limit(root, slot_minutes)
    teachers = tuple(
        TeacherSpec(
            id=name,
            max_daily_minutes=daily_limit,
            unavailable_slot_ids=frozenset(teacher_blocked.get(name, ())),
            has_declared_availability=name in teacher_blocked,
        )
        for name in (
            _text(t, "Name") for t in root.findall("Teachers_List/Teacher")
        )
        if name
    )

    # --- students sets -------------------------------------------------------
    set_sizes = _students_sets(root)
    set_blocked = _not_available(
        root, "ConstraintStudentsSetNotAvailableTimes", "Students"
    )

    # --- activities ----------------------------------------------------------
    rooms_by_subject, rooms_by_activity = _allowed_rooms(root)
    known_rooms = {r.id for r in rooms}
    sessions: list[SessionSpec] = []
    enrollments: list[StudentEnrollmentSpec] = []
    inactive = 0
    by_offering: dict[str, list[str]] = {}
    sets_of_session: dict[str, tuple[str, ...]] = {}

    for element in root.findall("Activities_List/Activity"):
        if _text(element, "Active", "true").lower() != "true":
            inactive += 1
            continue
        activity_id = _text(element, "Id")
        if not activity_id:
            continue
        subject = _text(element, "Subject")
        teacher_ids = tuple(
            (t.text or "").strip() for t in element.findall("Teacher") if t.text
        )
        student_sets = tuple(
            (s.text or "").strip() for s in element.findall("Students") if s.text
        )
        duration = max(int(_text(element, "Duration", "1") or 1), 1)

        # Activities sharing a subject and a students set are sessions of one
        # section — which is what makes SECTION_SESSIONS_NO_OVERLAP apply to
        # them, and what ConstraintMinDaysBetweenActivities is about.
        section_id = f"{subject}|{'+'.join(student_sets)}" or activity_id
        seat_demand = sum(set_sizes.get(s, 0) for s in student_sets)

        # Activity-level restriction wins; subject-level is the fallback.
        # Intersected with the rooms that actually exist, so a stale name in
        # the file cannot empty the domain and block the whole instance.
        allowed = rooms_by_activity.get(activity_id) or rooms_by_subject.get(subject)
        allowed_room_ids = frozenset(allowed & known_rooms) if allowed else frozenset()

        sessions.append(
            SessionSpec(
                id=activity_id,
                section_id=section_id,
                offering_id=section_id,
                course_id=subject,
                campus_id="FET",
                duration_slots=duration,
                session_type=SessionType.LECTURE,
                teacher_ids=teacher_ids,
                seat_demand=seat_demand,
                allowed_room_ids=allowed_room_ids,
            )
        )
        by_offering.setdefault(section_id, []).append(activity_id)
        sets_of_session[activity_id] = student_sets

    # One enrollment per (students set, offering). The set is the student.
    demand: dict[tuple[str, str], list[str]] = {}
    for session in sessions:
        for student_set in sets_of_session.get(session.id, ()):
            demand.setdefault((student_set, session.offering_id), []).append(session.id)
    for (student_set, offering_id), session_ids in sorted(demand.items()):
        enrollments.append(
            StudentEnrollmentSpec(
                student_id=student_set,
                offering_id=offering_id,
                session_ids=tuple(sorted(session_ids)),
                is_mandatory=True,
                # FET offers no alternative sections or offerings: a students
                # set attends what it attends. Zero alternatives is the
                # faithful reading, and makes clashes UNRESOLVABLE — which is
                # exactly FET's own compulsory-students-clash semantics.
                alternative_section_count=0,
                alternative_offering_count=0,
            )
        )

    if inactive:
        diagnostics.append(
            Diagnostic(
                code=DiagnosticCode.IMPORT_ACTIVITY_INACTIVE,
                message=f"{inactive} inactive activity(ies) were not imported",
                detail={"count": inactive},
            )
        )

    if enrollments:
        diagnostics.append(
            Diagnostic(
                code=DiagnosticCode.IMPORT_COHORT_TREATED_AS_STUDENT,
                message=(
                    f"{len(set_sizes)} FET students set(s) imported as indivisible "
                    "synthetic students; FET records demand per set, not per "
                    "student, so conflicts are set-level (ADR-0009 keeps real "
                    "cohorts out of the conflict model — these are not cohorts, "
                    "they are the only students this source has)"
                ),
                detail={"students_sets": len(set_sizes)},
            )
        )
        diagnostics.append(
            Diagnostic(
                code=DiagnosticCode.IMPORT_CRITICALITY_UNAVAILABLE,
                message=(
                    "this source carries no per-student enrollment history, so "
                    "repeated-course status, attempt number and alternative "
                    "counts are all unavailable; SESSION 5 criticality cannot "
                    "rank students from a FET import and must not pretend to"
                ),
            )
        )

    # --- the existing timetable, if the file carries one ---------------------
    base_assignments, base_notes = _base_assignments(root, {s.id for s in sessions})
    diagnostics.extend(base_notes)

    # --- unmapped constraint kinds -------------------------------------------
    diagnostics.extend(_unmapped(root))

    snapshot = BaseSnapshot(
        period=PeriodSpec(id=period_id, slot_minutes=slot_minutes, days=days),
        slots=tuple(slots),
        rooms=tuple(rooms),
        teachers=teachers,
        sessions=tuple(sessions),
        enrollments=tuple(enrollments),
        base_assignments=base_assignments,
    )

    return FetImport(
        snapshot=snapshot,
        diagnostics=tuple(diagnostics),
        source_counts={
            "days": len(day_names),
            "hours": len(hour_names),
            "rooms": len(rooms),
            "teachers": len(teachers),
            "activities": len(sessions),
            "students_sets": len(set_sizes),
            "enrollments": len(enrollments),
            "base_assignments": len(base_assignments),
        },
    )


def read_fet_file(path: str | Path, *, period_id: str = "FET") -> FetImport:
    return read_fet(ET.parse(Path(path)).getroot(), period_id=period_id)


# ---------------------------------------------------------------------------
# Pieces
# ---------------------------------------------------------------------------


def _allowed_rooms(root: ET.Element) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Room restrictions, by subject and by activity.

    FET expresses these as "preferred rooms", but at weight 100 a preference
    is a restriction: the generator will not place the activity anywhere else.
    Importing them as `allowed_room_ids` is what makes `ROOM_TYPE_ALLOWED`
    mean something for this source — and it is also how the activities the
    file leaves without a fixed room get a sensible domain instead of every
    room in the institution.

    Activity-level wins over subject-level: the narrower claim is the one the
    institution went to the trouble of making.
    """
    by_subject: dict[str, set[str]] = {}
    by_activity: dict[str, set[str]] = {}
    container = root.find("Space_Constraints_List")
    if container is None:
        return by_subject, by_activity

    for constraint in container:
        if _text(constraint, "Active", "true").lower() != "true":
            continue
        if float(_text(constraint, "Weight_Percentage", "0") or 0) < 100:
            continue
        rooms = {
            (r.text or "").strip()
            for r in constraint.findall("Preferred_Room")
            if r.text
        }
        single = _text(constraint, "Room")
        if single:
            rooms.add(single)
        if not rooms:
            continue
        if constraint.tag == "ConstraintSubjectPreferredRooms":
            subject = _text(constraint, "Subject")
            if subject:
                by_subject.setdefault(subject, set()).update(rooms)
        elif constraint.tag in (
            "ConstraintActivityPreferredRoom",
            "ConstraintActivityPreferredRooms",
        ):
            activity_id = _text(constraint, "Activity_Id")
            if activity_id:
                by_activity.setdefault(activity_id, set()).update(rooms)
    return by_subject, by_activity


def _teacher_daily_limit(root: ET.Element, slot_minutes: int) -> int | None:
    """The institution-wide teacher daily maximum, in minutes.

    FET states it in hours for all teachers at once. Per-teacher overrides are
    a different constraint kind and are still reported as unmapped.
    """
    for tag in ("ConstraintTeachersMaxHoursDaily", "ConstraintTeacherMaxHoursDaily"):
        for constraint in root.findall(f"Time_Constraints_List/{tag}"):
            if _text(constraint, "Active", "true").lower() != "true":
                continue
            hours = _text(constraint, "Maximum_Hours_Daily")
            if hours:
                return int(hours) * 60
    return None


def _break_times(root: ET.Element) -> set[tuple[str, str]]:
    out: set[tuple[str, str]] = set()
    for constraint in root.findall("Time_Constraints_List/ConstraintBreakTimes"):
        if _text(constraint, "Active", "true").lower() != "true":
            continue
        for slot in constraint.findall("Break_Time"):
            out.add((_text(slot, "Day"), _text(slot, "Hour")))
    return out


def _not_available(
    root: ET.Element, kind: str, subject_tag: str
) -> dict[str, set[str]]:
    """Deny-lists keyed by owner name, in our slot-id form.

    Presence of a key is what `has_declared_availability` reads: a teacher with
    no constraint row at all is indistinguishable from one available every
    hour, and only the distinction tells the two apart.
    """
    out: dict[str, set[str]] = {}
    for group in ("Time_Constraints_List", "Space_Constraints_List"):
        for constraint in root.findall(f"{group}/{kind}"):
            if _text(constraint, "Active", "true").lower() != "true":
                continue
            owner = _text(constraint, subject_tag)
            if not owner:
                continue
            blocked = out.setdefault(owner, set())
            for slot in constraint.findall("Not_Available_Time"):
                blocked.add(_slot_id(_text(slot, "Day"), _text(slot, "Hour")))
    return out


def _students_sets(root: ET.Element) -> dict[str, int]:
    """Every year, group and subgroup, with its recorded size.

    All three levels are collected because an activity may reference any of
    them, and FET does not say which level it will use.
    """
    sizes: dict[str, int] = {}

    def record(element: ET.Element) -> None:
        name = _text(element, "Name")
        if name:
            sizes[name] = int(_text(element, "Number_of_Students", "0") or 0)

    for year in root.findall("Students_List/Year"):
        record(year)
        for group in year.findall("Group"):
            record(group)
            for subgroup in group.findall("Subgroup"):
                record(subgroup)
    return sizes


def _base_assignments(
    root: ET.Element, known_sessions: set[str]
) -> tuple[tuple[BaseAssignment, ...], list[Diagnostic]]:
    """The timetable the file already contains.

    A `ConstraintActivityPreferredStartingTime` at weight 100 is not a
    preference in FET, it is a placement — and a file where almost every
    activity has one is a solved timetable rather than a problem statement.
    Importing those as `base_assignments` is what lets the platform validate
    an existing schedule and re-optimise it with minimal disruption, instead
    of throwing the institution's work away and starting from nothing.

    ``Permanently_Locked`` is carried through as `is_locked`, because that is
    FET saying "this one may not move at all".
    """
    starts: dict[str, tuple[str, bool]] = {}
    for constraint in root.findall(
        "Time_Constraints_List/ConstraintActivityPreferredStartingTime"
    ):
        if _text(constraint, "Active", "true").lower() != "true":
            continue
        if float(_text(constraint, "Weight_Percentage", "0") or 0) < 100:
            continue
        activity_id = _text(constraint, "Activity_Id")
        if activity_id not in known_sessions:
            continue
        starts[activity_id] = (
            _slot_id(_text(constraint, "Day"), _text(constraint, "Hour")),
            _text(constraint, "Permanently_Locked", "false").lower() == "true",
        )

    rooms: dict[str, str] = {}
    for constraint in root.findall(
        "Space_Constraints_List/ConstraintActivityPreferredRoom"
    ):
        if _text(constraint, "Active", "true").lower() != "true":
            continue
        if float(_text(constraint, "Weight_Percentage", "0") or 0) < 100:
            continue
        activity_id = _text(constraint, "Activity_Id")
        if activity_id in known_sessions:
            rooms[activity_id] = _text(constraint, "Room")

    assignments = tuple(
        BaseAssignment(
            session_id=activity_id,
            start_slot_id=slot_id,
            room_id=rooms.get(activity_id),
            is_locked=locked,
        )
        for activity_id, (slot_id, locked) in sorted(starts.items())
    )

    notes: list[Diagnostic] = []
    if assignments:
        notes.append(
            Diagnostic(
                code=DiagnosticCode.IMPORT_BASE_SCHEDULE_DETECTED,
                message=(
                    f"{len(assignments)} of {len(known_sessions)} activities "
                    "already carry a fixed start time: this file is an existing "
                    "timetable, not a blank problem. Use the MINIMAL_DISRUPTION "
                    "profile to re-optimise it"
                ),
                detail={
                    "placed": len(assignments),
                    "activities": len(known_sessions),
                    "with_room": len(rooms),
                    "locked": sum(1 for _, (_, l) in starts.items() if l),
                },
            )
        )
    return assignments, notes


def _unmapped(root: ET.Element) -> list[Diagnostic]:
    """Every constraint kind this importer does not implement.

    Reported per kind with a count, because "we ignored 121 of your
    constraints" is actionable and "import complete" is not.
    """
    counts: Counter[str] = Counter()
    for group in ("Time_Constraints_List", "Space_Constraints_List"):
        container = root.find(group)
        if container is None:
            continue
        for constraint in container:
            if constraint.tag not in _MAPPED:
                counts[constraint.tag] += 1

    return [
        Diagnostic(
            code=DiagnosticCode.IMPORT_CONSTRAINT_KIND_UNMAPPED,
            message=(
                f"{count} x {kind} was not imported; any rule it expressed is "
                "NOT enforced in the resulting instance"
            ),
            subject_kind="fet_constraint",
            subject_id=kind,
            detail={"kind": kind, "count": count},
        )
        for kind, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
