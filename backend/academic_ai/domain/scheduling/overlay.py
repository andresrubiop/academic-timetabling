"""Applying a scenario's perturbations to base data (SESSION 11).

ADR-0007 decided that a scenario is an **overlay**, not a copy: the base data
stays authoritative and a what-if is a list of declarative changes applied on
top. `ProblemBuilder` has carried that contract since SESSION 2 with nobody
implementing it. This is the implementation.

Why a copy would have been wrong
================================

Copying the whole timetable per scenario means a coordinator exploring five
options has five snapshots that drift from base data the moment anybody edits
an enrolment. Answers computed against a stale copy look authoritative and are
not, and there is no moment at which the divergence announces itself.

An overlay re-derives from live base data every time it is applied, so a
scenario is always a question about the *current* timetable.

Three properties, each load-bearing
===================================

**Pure.** `apply_overlay` takes a snapshot and returns a new one. It never
mutates its input, so the base a scenario was built from is still the base
afterwards — which is what lets the diff in `diff.py` compare them.

**Ordered.** Perturbations apply in `sequence_number` order and later ones see
the results of earlier ones. "Make Ana unavailable on Tuesday, then move her
class" is a different question from the same pair reversed, and the order is
data rather than an accident of iteration.

**Loud when it cannot comply.** A perturbation naming a session that does not
exist raises. The alternative — skipping it — produces a scenario that answers
a question nobody asked, and reports the answer with the same confidence as a
real one.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Callable, Mapping

from academic_ai.domain.scheduling.problem import (
    BaseAssignment,
    BaseSnapshot,
    Overlay,
    Perturbation,
    RoomSpec,
    SessionSpec,
    StudentEnrollmentSpec,
    TeacherSpec,
)

__all__ = [
    "PERTURBATION_KINDS",
    "OverlayError",
    "apply_overlay",
    "affected_sessions",
]


class OverlayError(ValueError):
    """A perturbation that cannot be applied as stated.

    Always names the perturbation and its target. "Invalid scenario" tells the
    coordinator who built it nothing they can act on.
    """


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise OverlayError(message)


def _target(perturbation: Perturbation, key: str) -> str:
    value = perturbation.targets.get(key)
    _require(
        bool(value),
        f"{perturbation.kind} (step {perturbation.sequence_number}) needs a "
        f"{key!r} target",
    )
    return str(value)


# ---------------------------------------------------------------------------
# The perturbations
# ---------------------------------------------------------------------------


def _move_session(snapshot: BaseSnapshot, p: Perturbation) -> BaseSnapshot:
    """Pin a session somewhere. The drag from SESSION 10, as a scenario.

    Implemented as a *base assignment*, not as a constraint: the session is
    where the scenario says it is, and the optimizer is free to reason about
    everything else around it. Making it a HARD constraint instead would be a
    different question — "can this work with the class nailed there" rather
    than "what happens if we put it there".
    """
    session_id = _target(p, "session_id")
    known = {s.id for s in snapshot.sessions}
    _require(
        session_id in known,
        f"MOVE_SESSION names session {session_id!r}, which is not in this period",
    )

    slot_id = str(p.payload.get("start_slot_id") or "")
    _require(bool(slot_id), f"MOVE_SESSION for {session_id} needs a start_slot_id")
    slot_ids = {s.id for s in snapshot.slots}
    _require(
        slot_id in slot_ids,
        f"MOVE_SESSION for {session_id} names slot {slot_id!r}, which is not in "
        "this period's grid",
    )

    room_id = p.payload.get("room_id")
    if room_id is not None:
        room_ids = {r.id for r in snapshot.rooms}
        _require(
            str(room_id) in room_ids,
            f"MOVE_SESSION for {session_id} names room {room_id!r}, which does "
            "not exist",
        )

    existing = {b.session_id: b for b in snapshot.base_assignments}
    previous = existing.get(session_id)
    existing[session_id] = BaseAssignment(
        session_id=session_id,
        start_slot_id=slot_id,
        # None means "leave the room alone" -- the same rule as a drag along
        # the time axis in SESSION 10.
        room_id=str(room_id) if room_id is not None else (previous.room_id if previous else None),
    )
    return replace(snapshot, base_assignments=tuple(existing.values()))


def _teacher_unavailable(snapshot: BaseSnapshot, p: Perturbation) -> BaseSnapshot:
    """Add slots to a teacher's deny-list. The "Ana is on leave" question."""
    teacher_id = _target(p, "teacher_id")
    teachers = {t.id: t for t in snapshot.teachers}
    _require(
        teacher_id in teachers,
        f"TEACHER_UNAVAILABLE names teacher {teacher_id!r}, who is not in this "
        "period",
    )

    slots = _slot_selection(snapshot, p)
    teacher = teachers[teacher_id]
    teachers[teacher_id] = replace(
        teacher,
        unavailable_slot_ids=teacher.unavailable_slot_ids | slots,
        # A teacher the scenario has an opinion about has declared
        # availability by definition, whatever the base data said. Leaving
        # this False would let the "no availability on record" warning fire
        # for somebody the coordinator just told us about.
        has_declared_availability=True,
    )
    return replace(snapshot, teachers=tuple(teachers.values()))


def _room_unavailable(snapshot: BaseSnapshot, p: Perturbation) -> BaseSnapshot:
    """Take a room out, wholly or for particular slots."""
    room_id = _target(p, "room_id")
    rooms = {r.id: r for r in snapshot.rooms}
    _require(room_id in rooms, f"ROOM_UNAVAILABLE names room {room_id!r}, which does not exist")

    slots = _slot_selection(snapshot, p, default_all=False)
    room = rooms[room_id]
    if not slots:
        # No slots named means the room is gone for the whole period -- a
        # refurbishment, not a one-off booking.
        rooms[room_id] = replace(room, is_active=False)
    else:
        rooms[room_id] = replace(
            room,
            unavailable_slot_ids=getattr(room, "unavailable_slot_ids", frozenset()) | slots,
            has_declared_availability=True,
        )
    return replace(snapshot, rooms=tuple(rooms.values()))


def _change_instructor(snapshot: BaseSnapshot, p: Perturbation) -> BaseSnapshot:
    """Swap who teaches a session."""
    session_id = _target(p, "session_id")
    sessions = {s.id: s for s in snapshot.sessions}
    _require(session_id in sessions, f"CHANGE_INSTRUCTOR names unknown session {session_id!r}")

    raw = p.payload.get("teacher_ids")
    new_teachers = tuple(str(t) for t in raw) if isinstance(raw, (list, tuple)) else ()
    _require(
        bool(new_teachers),
        f"CHANGE_INSTRUCTOR for {session_id} needs teacher_ids. An empty list "
        "would leave the class with nobody to teach it, which is a different "
        "scenario and should be stated as one.",
    )
    known = {t.id for t in snapshot.teachers}
    unknown = sorted(set(new_teachers) - known)
    _require(
        not unknown,
        f"CHANGE_INSTRUCTOR for {session_id} names teacher(s) not in this "
        f"period: {', '.join(unknown)}",
    )

    sessions[session_id] = replace(sessions[session_id], teacher_ids=new_teachers)
    return replace(snapshot, sessions=tuple(sessions.values()))


def _change_room_requirement(snapshot: BaseSnapshot, p: Perturbation) -> BaseSnapshot:
    """Change what a session needs from a room: features, seats, or a room at all."""
    session_id = _target(p, "session_id")
    sessions = {s.id: s for s in snapshot.sessions}
    _require(session_id in sessions, f"CHANGE_ROOM_REQUIREMENT names unknown session {session_id!r}")

    session = sessions[session_id]
    changes: dict[str, object] = {}
    if "required_features" in p.payload:
        raw = p.payload["required_features"]
        _require(
            isinstance(raw, Mapping),
            f"CHANGE_ROOM_REQUIREMENT for {session_id}: required_features must "
            "be a mapping of feature id to quantity",
        )
        changes["required_features"] = dict(raw)  # type: ignore[arg-type]
    if "seat_demand" in p.payload:
        seats = int(p.payload["seat_demand"])  # type: ignore[arg-type]
        _require(seats >= 0, f"CHANGE_ROOM_REQUIREMENT for {session_id}: seat_demand cannot be negative")
        changes["seat_demand"] = seats
    if "requires_room" in p.payload:
        changes["requires_room"] = bool(p.payload["requires_room"])

    _require(
        bool(changes),
        f"CHANGE_ROOM_REQUIREMENT for {session_id} changes nothing. A "
        "perturbation that does nothing still counts as a scenario step and "
        "would make the diff claim a question was asked.",
    )
    sessions[session_id] = replace(session, **changes)  # type: ignore[arg-type]
    return replace(snapshot, sessions=tuple(sessions.values()))


def _change_campus_availability(snapshot: BaseSnapshot, p: Perturbation) -> BaseSnapshot:
    """Open or close a campus. Closing deactivates every room on it."""
    campus_id = _target(p, "campus_id")
    known = {r.campus_id for r in snapshot.rooms}
    _require(
        campus_id in known,
        f"CHANGE_CAMPUS_AVAILABILITY names campus {campus_id!r}, which has no "
        "rooms in this period",
    )
    available = bool(p.payload.get("is_available", False))
    rooms = tuple(
        replace(room, is_active=available) if room.campus_id == campus_id else room
        for room in snapshot.rooms
    )
    return replace(snapshot, rooms=rooms)


def _new_section(snapshot: BaseSnapshot, p: Perturbation) -> BaseSnapshot:
    """Open another section of an offering: the standard answer to "this
    course is full and everybody clashes".

    The new sessions are unplaced. That is the point -- the optimizer decides
    where they go, and a scenario that pinned them would be answering a
    narrower question than the one asked.
    """
    offering_id = _target(p, "offering_id")
    template = [s for s in snapshot.sessions if s.offering_id == offering_id]
    _require(
        bool(template),
        f"NEW_SECTION names offering {offering_id!r}, which has no sessions to "
        "copy the shape of",
    )

    section_code = str(p.payload.get("section_code") or f"{offering_id}-new")
    raw_teachers = p.payload.get("teacher_ids")
    teachers = tuple(str(t) for t in raw_teachers) if isinstance(raw_teachers, (list, tuple)) else None
    if teachers:
        known = {t.id for t in snapshot.teachers}
        unknown = sorted(set(teachers) - known)
        _require(not unknown, f"NEW_SECTION names unknown teacher(s): {', '.join(unknown)}")

    existing_ids = {s.id for s in snapshot.sessions}
    added: list[SessionSpec] = []
    for index, source in enumerate(template):
        new_id = f"{source.id}::{section_code}"
        _require(
            new_id not in existing_ids,
            f"NEW_SECTION would create session {new_id!r}, which already exists. "
            "Applying the same scenario step twice is a bug, not an increment.",
        )
        added.append(
            replace(
                source,
                id=new_id,
                section_id=f"{source.section_id}::{section_code}",
                teacher_ids=teachers if teachers else source.teacher_ids,
                # A new section is not part of any co-teaching group the
                # original belonged to: co-teaching is an arrangement between
                # named people about a named class.
                co_teaching_group_id=None,
            )
        )
    return replace(snapshot, sessions=snapshot.sessions + tuple(added))


def _merge_compatible_offerings(snapshot: BaseSnapshot, p: Perturbation) -> BaseSnapshot:
    """Teach two offerings as one class.

    Modelled as a **co-teaching group**, which is how the optimizer already
    expresses "these sessions happen together in one room". Merging by
    deleting one offering's sessions would lose the enrolments attached to
    them -- and the students, who still need to be somewhere.

    Seat demand is summed, because the merged class needs a room that holds
    everybody. `additive_features` handles the rest.
    """
    keep_id = _target(p, "offering_id")
    merge_id = str(p.payload.get("with_offering_id") or "")
    _require(bool(merge_id), "MERGE_COMPATIBLE_OFFERINGS needs with_offering_id")
    _require(
        keep_id != merge_id,
        "MERGE_COMPATIBLE_OFFERINGS was asked to merge an offering with itself",
    )

    keep = [s for s in snapshot.sessions if s.offering_id == keep_id]
    merge = [s for s in snapshot.sessions if s.offering_id == merge_id]
    _require(bool(keep), f"MERGE_COMPATIBLE_OFFERINGS: offering {keep_id!r} has no sessions")
    _require(bool(merge), f"MERGE_COMPATIBLE_OFFERINGS: offering {merge_id!r} has no sessions")
    _require(
        len(keep) == len(merge),
        f"MERGE_COMPATIBLE_OFFERINGS: {keep_id} has {len(keep)} session(s) and "
        f"{merge_id} has {len(merge)}. Merging offerings of different shapes "
        "needs a human decision about which session pairs with which.",
    )

    group_prefix = f"merge::{keep_id}::{merge_id}"
    # Paired by *id*, not by object: `SessionSpec` carries mappings and is
    # therefore unhashable, so zipping the specs into a dict raises.
    pairs = list(
        zip(sorted(keep, key=lambda s: s.id), sorted(merge, key=lambda s: s.id))
    )
    by_id = {s.id: s for s in snapshot.sessions}
    for index, (a, b) in enumerate(pairs):
        group_id = f"{group_prefix}::{index}"
        total_seats = a.seat_demand + b.seat_demand
        by_id[a.id] = replace(a, co_teaching_group_id=group_id, seat_demand=total_seats)
        by_id[b.id] = replace(b, co_teaching_group_id=group_id, seat_demand=total_seats)
    return replace(snapshot, sessions=tuple(by_id.values()))


def _lock_assignment(snapshot: BaseSnapshot, p: Perturbation) -> BaseSnapshot:
    """Pin a session where it already is. Handled by the LOCK adapter through
    the constraint set; here it only ensures a base assignment exists to pin
    to, because locking a session that was never placed pins it to nothing."""
    session_id = _target(p, "session_id")
    placed = {b.session_id for b in snapshot.base_assignments}
    _require(
        session_id in placed,
        f"LOCK_ASSIGNMENT names {session_id!r}, which has no assignment to lock. "
        "Locking an unplaced session pins it to nothing.",
    )
    return snapshot


def _unlock_assignment(snapshot: BaseSnapshot, p: Perturbation) -> BaseSnapshot:
    """Free a session the accepted schedule had pinned.

    Drops its base assignment, so nothing anchors it and the optimizer may put
    it anywhere its domain allows.
    """
    session_id = _target(p, "session_id")
    remaining = tuple(b for b in snapshot.base_assignments if b.session_id != session_id)
    _require(
        len(remaining) != len(snapshot.base_assignments),
        f"UNLOCK_ASSIGNMENT names {session_id!r}, which was not placed to begin "
        "with",
    )
    return replace(snapshot, base_assignments=remaining)


def _split_co_teaching_group(snapshot: BaseSnapshot, p: Perturbation) -> BaseSnapshot:
    """Stop teaching two classes together."""
    group_id = _target(p, "co_teaching_group_id")
    members = [s for s in snapshot.sessions if s.co_teaching_group_id == group_id]
    _require(bool(members), f"SPLIT_CO_TEACHING_GROUP names unknown group {group_id!r}")
    by_id = {s.id: s for s in snapshot.sessions}
    for member in members:
        by_id[member.id] = replace(member, co_teaching_group_id=None)
    return replace(
        snapshot,
        sessions=tuple(by_id.values()),
        co_teaching_groups=tuple(
            g for g in snapshot.co_teaching_groups if g.id != group_id
        ),
    )


def _slot_selection(
    snapshot: BaseSnapshot, p: Perturbation, *, default_all: bool = True
) -> frozenset[str]:
    """Which slots a perturbation refers to.

    ``slot_ids`` names them outright; ``days`` names whole days, which is how
    a coordinator actually says "Ana cannot do Tuesdays". Neither given means
    the whole period when `default_all`, and nothing when not.
    """
    explicit = p.payload.get("slot_ids")
    if isinstance(explicit, (list, tuple)) and explicit:
        known = {s.id for s in snapshot.slots}
        unknown = sorted({str(s) for s in explicit} - known)
        _require(
            not unknown,
            f"{p.kind} names slot(s) not in this period's grid: "
            f"{', '.join(unknown[:5])}",
        )
        return frozenset(str(s) for s in explicit)

    days = p.payload.get("days")
    if isinstance(days, (list, tuple)) and days:
        wanted = {str(d).upper() for d in days}
        selected = frozenset(
            slot.id for slot in snapshot.slots if slot.day_of_week.value.upper() in wanted
        )
        _require(
            bool(selected),
            f"{p.kind} names day(s) {', '.join(sorted(wanted))}, which this "
            "period's grid does not contain",
        )
        return selected

    if default_all:
        return frozenset(slot.id for slot in snapshot.slots)
    return frozenset()


PERTURBATION_KINDS: Mapping[str, Callable[[BaseSnapshot, Perturbation], BaseSnapshot]] = {
    "MOVE_SESSION": _move_session,
    "TEACHER_UNAVAILABLE": _teacher_unavailable,
    "ROOM_UNAVAILABLE": _room_unavailable,
    "NEW_SECTION": _new_section,
    "MERGE_COMPATIBLE_OFFERINGS": _merge_compatible_offerings,
    "SPLIT_CO_TEACHING_GROUP": _split_co_teaching_group,
    "CHANGE_INSTRUCTOR": _change_instructor,
    "CHANGE_ROOM_REQUIREMENT": _change_room_requirement,
    "CHANGE_CAMPUS_AVAILABILITY": _change_campus_availability,
    "LOCK_ASSIGNMENT": _lock_assignment,
    "UNLOCK_ASSIGNMENT": _unlock_assignment,
}


def apply_overlay(base: BaseSnapshot, overlay: Overlay) -> BaseSnapshot:
    """Base data with the scenario's perturbations applied, in order.

    Returns a new snapshot; ``base`` is untouched, which is what lets
    `diff.py` compare the two afterwards.
    """
    snapshot = base
    for perturbation in overlay.in_order():
        handler = PERTURBATION_KINDS.get(perturbation.kind)
        if handler is None:
            raise OverlayError(
                f"unknown perturbation kind {perturbation.kind!r} (step "
                f"{perturbation.sequence_number}). Refusing rather than "
                "skipping: a scenario that silently drops a step answers a "
                "question nobody asked."
            )
        snapshot = handler(snapshot, perturbation)
    return snapshot


def affected_sessions(base: BaseSnapshot, overlay: Overlay) -> frozenset[str]:
    """Sessions an overlay touches directly.

    The seed for minimal-disruption re-optimisation: LNS frees this region and
    its neighbours rather than the whole timetable, because re-solving
    everything to accommodate one unavailable teacher would move classes that
    had no reason to move.
    """
    touched: set[str] = set()
    by_offering: dict[str, list[str]] = {}
    for session in base.sessions:
        by_offering.setdefault(session.offering_id, []).append(session.id)

    for p in overlay.in_order():
        session_id = p.targets.get("session_id")
        if session_id:
            touched.add(str(session_id))
        offering_id = p.targets.get("offering_id")
        if offering_id:
            touched.update(by_offering.get(str(offering_id), ()))
        with_offering = p.payload.get("with_offering_id")
        if with_offering:
            touched.update(by_offering.get(str(with_offering), ()))
        teacher_id = p.targets.get("teacher_id")
        if teacher_id:
            touched.update(
                s.id for s in base.sessions if teacher_id in s.teacher_ids
            )
        room_id = p.targets.get("room_id")
        if room_id:
            touched.update(
                b.session_id for b in base.base_assignments if b.room_id == room_id
            )
        campus_id = p.targets.get("campus_id")
        if campus_id:
            on_campus = {r.id for r in base.rooms if r.campus_id == campus_id}
            touched.update(
                b.session_id for b in base.base_assignments if b.room_id in on_campus
            )
        group_id = p.targets.get("co_teaching_group_id")
        if group_id:
            touched.update(
                s.id for s in base.sessions if s.co_teaching_group_id == group_id
            )
    return frozenset(touched)
