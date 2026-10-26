"""Constraint adapters: one per catalog code, registered by name.

`missing_adapters()` is the safety property this module exists to satisfy. A
HARD constraint with no adapter does not fail loudly — it silently is not
enforced, and CP-SAT returns a confident OPTIMAL for a schedule that
double-books a room. So every code in `solver_hard_codes()` must appear in
`REGISTRY`, and `tests/test_adapters.py` fails if one does not.

Two kinds of adapter
====================

**Domain-enforced.** Everything depending on one session alone — day fit,
schedulable slots, teacher availability, room active/capacity/campus/features
— is already enforced by `compute_domains()` having excluded the offending
values before a variable existed. Its adapter posts nothing and says so.
That is not a loophole: the constraint *is* enforced, more cheaply and with a
far better diagnostic than a posted constraint would give. What matters is
that it is registered, so the completeness check sees it, and that it declares
`enforced_by_domain` rather than quietly doing nothing.

**Solver-posted.** Everything involving two or more sessions — the
disjunctions, co-teaching, travel, locks. These are real CP-SAT constraints.

Scope
=====

An adapter that can be scoped resolves its constraint per use through
`instance.resolve(code, context)`, so a room-capacity override attached to one
auditorium stays attached to that auditorium.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Mapping, Protocol, Sequence

from ortools.sat.python import cp_model

from academic_ai.domain.enums import ConstraintCategory
from academic_ai.domain.scheduling.catalog import CONSTRAINT_CATALOG, solver_hard_codes
from academic_ai.domain.scheduling.problem import classify_student_conflict
from academic_ai.domain.scheduling.scope import ScopeContext
from academic_ai.optimization.variables import ModelVariables

__all__ = [
    "REGISTRY",
    "AdapterResult",
    "ConstraintAdapter",
    "apply_hard_constraints",
    "registered_codes",
]


@dataclass(frozen=True, slots=True)
class AdapterResult:
    """What an adapter did, for the run log."""

    code: str
    posted: int
    enforced_by_domain: bool = False
    note: str = ""


class ConstraintAdapter(Protocol):
    def __call__(self, v: ModelVariables) -> AdapterResult: ...


def _adapter(code: str) -> Callable[[ConstraintAdapter], ConstraintAdapter]:
    def register(fn: ConstraintAdapter) -> ConstraintAdapter:
        if code not in CONSTRAINT_CATALOG:
            raise ValueError(f"adapter for unknown constraint {code!r}")
        REGISTRY[code] = fn
        return fn

    return register


REGISTRY: dict[str, ConstraintAdapter] = {}


def _domain_enforced(code: str, note: str) -> None:
    """Register an adapter for a constraint the variable domains already
    enforce."""

    def adapter(v: ModelVariables) -> AdapterResult:
        return AdapterResult(code=code, posted=0, enforced_by_domain=True, note=note)

    adapter.__name__ = f"adapter_{code.lower()}"
    REGISTRY[code] = adapter


# ---------------------------------------------------------------------------
# Domain-enforced
# ---------------------------------------------------------------------------

_domain_enforced(
    "SESSION_WITHIN_DAY_GRID",
    "start domain contains only slots where the whole span fits in one day",
)
_domain_enforced(
    "SESSION_SLOTS_SCHEDULABLE",
    "start domain excludes any span covering an unschedulable slot",
)
_domain_enforced(
    "TEACHER_AVAILABILITY",
    "start domain excludes spans where any assigned teacher is unavailable",
)
_domain_enforced("ROOM_ACTIVE", "room domain contains only active rooms")
_domain_enforced(
    "ROOM_CAPACITY",
    "room domain excludes rooms too small for the session or its co-teaching "
    "group, unless a named human downgraded the constraint",
)
_domain_enforced(
    "ROOM_CAMPUS_MATCH", "room domain contains only rooms on the offering's campus"
)
_domain_enforced(
    "ROOM_FEATURE_REQUIREMENT",
    "room domain contains only rooms carrying every required feature",
)
_domain_enforced(
    "ROOM_TYPE_ALLOWED",
    "room domain is intersected with the session's allowed_room_ids when it "
    "has one; an empty list means unrestricted",
)


# ---------------------------------------------------------------------------
# Disjunctions
# ---------------------------------------------------------------------------


def _group_intervals(v: ModelVariables) -> tuple[dict[str, object], set[str]]:
    """One interval per co-teaching group, and the members it stands for.

    A merged offering is one class: it occupies one room, and its teachers'
    time, once. So the resource disjunctions must count the group once —
    otherwise `CO_TEACHING_SAME_TIMESLOT` and `CO_TEACHING_SAME_ROOM`
    contradict them and every approved merge is reported INFEASIBLE.

    The first version did that by keeping ``members[0]`` and dropping the
    rest. That was wrong in three ways at once, because nothing in the data
    model forces a group's members to agree: a member with a **longer
    duration** had its tail unprotected, a member with a **different teacher**
    left that teacher unconstrained, and if the representative needed no room
    while a peer did, the two adapters picked different representatives and
    *no* member entered the room disjunction at all. The answer even depended
    on the order of ``member_session_ids`` — the same data, listed the other
    way round, gave INFEASIBLE instead of a double-booked OPTIMAL.

    The group interval fixes all three by construction: it starts where the
    members start (they are equated) and spans the **longest** member, so it
    is a faithful statement of what the merged class actually occupies.
    """
    intervals: dict[str, object] = {}
    represented: set[str] = set()
    for group in v.instance.snapshot.co_teaching_groups:
        members = [m for m in group.member_session_ids if m in v.start]
        if len(members) < 2:
            continue
        span = max(v.sessions[m].duration_slots for m in members)
        intervals[group.id] = v.model.NewFixedSizeIntervalVar(
            v.start[members[0]], span, f"group[{group.id}]"
        )
        represented.update(members)
    return intervals, represented


def _group_of(v: ModelVariables) -> dict[str, str]:
    out: dict[str, str] = {}
    for group in v.instance.snapshot.co_teaching_groups:
        for member in group.member_session_ids:
            if member in v.start:
                out[member] = group.id
    return out


@_adapter("TEACHER_NO_OVERLAP")
def teacher_no_overlap(v: ModelVariables) -> AdapterResult:
    """One teacher cannot be in two places at once.

    `AddNoOverlap` rather than pairwise: the occupancy index makes ranges on
    different days disjoint by construction, so a single disjunction per
    teacher is both correct and linear in sessions.
    """
    group_intervals, represented = _group_intervals(v)
    group_of = _group_of(v)
    teachers = {
        t
        for sid in v.scheduled_ids()
        for t in v.sessions[sid].teacher_ids
    }
    posted = 0
    for teacher_id in sorted(teachers):
        owned = v.sessions_of_teacher(teacher_id)
        intervals = []
        seen_groups: set[str] = set()
        for sid in owned:
            group_id = group_of.get(sid)
            if group_id is None or sid not in represented:
                intervals.append(v.interval[sid])
                continue
            # Any teacher of any member has the whole group's span occupied,
            # counted once.
            if group_id not in seen_groups:
                seen_groups.add(group_id)
                intervals.append(group_intervals[group_id])
        if len(intervals) < 2:
            continue
        v.model.AddNoOverlap(intervals)
        posted += 1
    skip = represented
    return AdapterResult(
        "TEACHER_NO_OVERLAP",
        posted,
        note=(
            f"{len(skip)} co-taught session(s) counted through a group peer"
            if skip
            else ""
        ),
    )


@_adapter("ROOM_NO_OVERLAP")
def room_no_overlap(v: ModelVariables) -> AdapterResult:
    """A room holds one class at a time.

    Optional intervals: the interval for (session, room) is present exactly
    when that room is chosen, so one disjunction per room covers every
    possible assignment.
    """
    # A co-taught group occupies one room once, for its longest member's span.
    group_intervals, represented = _group_intervals(v)
    group_of = _group_of(v)

    # One optional interval per (group, candidate room), present exactly when
    # any member is in that room. `CO_TEACHING_SAME_ROOM` equates the members'
    # room literals, so any member's literal identifies the group's room.
    group_room: dict[tuple[str, str], object] = {}
    for group in v.instance.snapshot.co_teaching_groups:
        members = [m for m in group.member_session_ids if m in v.room_lit]
        if not members or group.id not in group_intervals:
            continue
        anchor = members[0]
        span = max(v.sessions[m].duration_slots for m in members)
        for room_id, lit in v.room_lit[anchor].items():
            group_room[(group.id, room_id)] = (
                v.model.NewOptionalFixedSizeIntervalVar(
                    v.start[anchor], span, lit, f"group[{group.id},{room_id}]"
                )
            )

    posted = 0
    for room_id in sorted(v.rooms):
        intervals = [
            interval
            for (sid, rid), interval in v.room_interval.items()
            if rid == room_id and sid not in represented
        ]
        intervals += [
            interval
            for (gid, rid), interval in group_room.items()
            if rid == room_id
        ]
        if len(intervals) < 2:
            continue
        v.model.AddNoOverlap(intervals)
        posted += 1
    skip = represented
    return AdapterResult(
        "ROOM_NO_OVERLAP",
        posted,
        note=(
            f"{len(skip)} co-taught session(s) represented by a group peer"
            if skip
            else ""
        ),
    )


@_adapter("SECTION_SESSIONS_NO_OVERLAP")
def section_sessions_no_overlap(v: ModelVariables) -> AdapterResult:
    """Two sessions of one section are attended by the same bodies.

    The model's only enrollment-independent backstop: it holds even when every
    enrollment is unplaced, which is the majority case at planning time.
    """
    by_section: dict[str, list[str]] = {}
    for sid in v.scheduled_ids():
        by_section.setdefault(v.sessions[sid].section_id, []).append(sid)
    posted = 0
    for section_id, sids in sorted(by_section.items()):
        if len(sids) < 2:
            continue
        v.model.AddNoOverlap([v.interval[s] for s in sids])
        posted += 1
    return AdapterResult("SECTION_SESSIONS_NO_OVERLAP", posted)


@_adapter("STUDENT_NO_OVERLAP_UNRESOLVABLE")
def student_no_overlap_unresolvable(v: ModelVariables) -> AdapterResult:
    """Clashes no registrar action can resolve.

    Only the UNRESOLVABLE class is posted as HARD. The resolvable ones are
    objective terms — SESSION 5 is explicit that not every student conflict
    may be promoted to HARD, and doing so would report INFEASIBLE for
    timetables a registrar could fix by moving one student.

    Pairs are aggregated: one constraint per distinct session pair, however
    many students share it.
    """
    by_student: dict[str, list] = {}
    for enrollment in v.instance.snapshot.enrollments:
        by_student.setdefault(enrollment.student_id, []).append(enrollment)

    posted = 0
    pairs: set[tuple[str, str]] = set()

    for enrollments in by_student.values():
        scheduled = [
            e for e in enrollments if any(s in v.start for s in e.session_ids)
        ]
        if len(scheduled) < 2:
            continue

        classes = {
            classify_student_conflict(a, b)
            for i, a in enumerate(scheduled)
            for b in scheduled[i + 1 :]
        }
        sessions = sorted(
            {s for e in scheduled for s in e.session_ids if s in v.start}
        )

        if classes == {"STUDENT_NO_OVERLAP_UNRESOLVABLE"} and len(sessions) > 1:
            # Every pair of this student's enrollments is unresolvable, so all
            # their sessions are mutually disjoint — one disjunction says that
            # in O(n) instead of O(n²) pairwise constraints. On the imported
            # FET instance this replaces 2 558 pairwise constraints with 52.
            v.model.AddNoOverlap([v.interval[s] for s in sessions])
            posted += 1
            continue

        # Mixed: some pairs are resolvable and belong in the objective, not
        # here. Fall back to naming the unresolvable pairs individually.
        for i, a in enumerate(scheduled):
            for b in scheduled[i + 1 :]:
                if (
                    classify_student_conflict(a, b)
                    != "STUDENT_NO_OVERLAP_UNRESOLVABLE"
                ):
                    continue
                for sa in a.session_ids:
                    for sb in b.session_ids:
                        if sa == sb or sa not in v.start or sb not in v.start:
                            continue
                        pairs.add(tuple(sorted((sa, sb))))  # type: ignore[arg-type]

    for sa, sb in sorted(pairs):
        v.model.AddNoOverlap([v.interval[sa], v.interval[sb]])
        posted += 1
    return AdapterResult("STUDENT_NO_OVERLAP_UNRESOLVABLE", posted)


# ---------------------------------------------------------------------------
# Co-teaching
# ---------------------------------------------------------------------------


@_adapter("CO_TEACHING_SAME_TIMESLOT")
def co_teaching_same_timeslot(v: ModelVariables) -> AdapterResult:
    posted = 0
    for group in v.instance.snapshot.co_teaching_groups:
        members = [m for m in group.member_session_ids if m in v.start]
        for other in members[1:]:
            v.model.Add(v.start[other] == v.start[members[0]])
            posted += 1
    return AdapterResult("CO_TEACHING_SAME_TIMESLOT", posted)


@_adapter("CO_TEACHING_SAME_ROOM")
def co_teaching_same_room(v: ModelVariables) -> AdapterResult:
    """Members share one room — which is what makes the merge a merge.

    Note the interaction with `ROOM_NO_OVERLAP`: co-taught sessions occupy the
    same room at the same time, which a naive no-overlap would forbid. The
    room disjunction is therefore relaxed for members of a group, and the
    seat demand they must satisfy is their *sum* (handled in domain pruning).
    """
    posted = 0
    for group in v.instance.snapshot.co_teaching_groups:
        members = [m for m in group.member_session_ids if m in v.room_lit]
        if len(members) < 2:
            continue
        first = members[0]
        for other in members[1:]:
            for room_id in v.room_lit[first]:
                if room_id in v.room_lit[other]:
                    v.model.Add(
                        v.room_lit[other][room_id] == v.room_lit[first][room_id]
                    )
                    posted += 1
                else:
                    v.model.Add(v.room_lit[first][room_id] == 0)
    return AdapterResult("CO_TEACHING_SAME_ROOM", posted)


# ---------------------------------------------------------------------------
# Availability that depends on the room choice
# ---------------------------------------------------------------------------


@_adapter("ROOM_AVAILABILITY")
def room_availability(v: ModelVariables) -> AdapterResult:
    """A room closed for a slot cannot host a session spanning it.

    Depends on both variables, so it cannot be domain pruning: the room is
    only unusable *at particular times*.
    """
    posted = 0
    for session_id, room_lits in v.room_lit.items():
        duration = v.sessions[session_id].duration_slots
        for room_id, room_literal in room_lits.items():
            blocked = v.rooms[room_id].unavailable_slot_ids
            if not blocked:
                continue
            for slot_id, start_literal in v.start_lit[session_id].items():
                span_start = v.occupancy_index[slot_id]
                spanned = {
                    sid
                    for sid, occ in v.occupancy_index.items()
                    if span_start <= occ < span_start + duration
                }
                if spanned & blocked:
                    v.model.AddBoolOr(
                        [start_literal.Not(), room_literal.Not()]
                    )
                    posted += 1
    return AdapterResult("ROOM_AVAILABILITY", posted)


# ---------------------------------------------------------------------------
# Locks
# ---------------------------------------------------------------------------


@_adapter("ASSIGNMENT_LOCKED")
def assignment_locked(v: ModelVariables) -> AdapterResult:
    """A locked assignment keeps its slot and room."""
    posted = 0
    for assignment in v.instance.snapshot.base_assignments:
        if not assignment.is_locked:
            continue
        sid = assignment.session_id
        if sid not in v.start_lit:
            continue
        lits = v.start_lit[sid]
        if assignment.start_slot_id not in lits:
            # The pinned slot is not even in the domain: infeasible, and
            # saying so through the model is right — the lock is the thing
            # that cannot be honoured.
            v.model.AddBoolOr([lits[next(iter(lits))].Not()])
            v.model.Add(sum(lits.values()) == 0)
            posted += 1
            continue
        v.model.Add(lits[assignment.start_slot_id] == 1)
        posted += 1
        if assignment.room_id and sid in v.room_lit:
            room_lits = v.room_lit[sid]
            if assignment.room_id in room_lits:
                v.model.Add(room_lits[assignment.room_id] == 1)
                posted += 1
    return AdapterResult("ASSIGNMENT_LOCKED", posted)


@_adapter("SESSION_PINNED")
def session_pinned(v: ModelVariables) -> AdapterResult:
    """A durable institutional pin, distinct from a version-scoped lock.

    `is_locked` on an assignment says "LNS may not move this within this
    version". A pin says "the Dean froze this class for the year". Copying one
    forward as the other is the confusion this separation exists to prevent,
    so the pin is read from its own constraint parameters.
    """
    posted = 0
    for constraint in v.instance.constraints:
        if constraint.code != "SESSION_PINNED" or not constraint.is_active:
            continue
        params = constraint.parameters
        sid = constraint.scope.section_id or params.get("class_session_id")
        slot_id = params.get("start_slot_id")
        room_id = params.get("room_id")
        if not isinstance(sid, str) or sid not in v.start_lit:
            continue
        if isinstance(slot_id, str) and slot_id in v.start_lit[sid]:
            v.model.Add(v.start_lit[sid][slot_id] == 1)
            posted += 1
        if isinstance(room_id, str) and sid in v.room_lit:
            if room_id in v.room_lit[sid]:
                v.model.Add(v.room_lit[sid][room_id] == 1)
                posted += 1
    return AdapterResult("SESSION_PINNED", posted)


# ---------------------------------------------------------------------------
# Campus travel
# ---------------------------------------------------------------------------


def _travel_slots(v: ModelVariables, origin: str, destination: str) -> int:
    """Slots needed to get between two campuses.

    ``ceil``, never floor: rounding 45 minutes down to 0 on a 60-minute grid
    would schedule a journey nobody can make.
    """
    if origin == destination:
        return 0
    minutes = v.instance.snapshot.travel_minutes.get((origin, destination))
    if minutes is None:
        return 0
    return math.ceil(minutes / v.instance.snapshot.period.slot_minutes)


def _campus_literals(
    v: ModelVariables, session_id: str
) -> Mapping[str, list[cp_model.IntVar]]:
    """campus id -> the room literals that place this session on it."""
    out: dict[str, list[cp_model.IntVar]] = {}
    for room_id, lit in v.room_lit.get(session_id, {}).items():
        out.setdefault(v.rooms[room_id].campus_id, []).append(lit)
    return out


def _campus_indicator(
    v: ModelVariables,
    session_id: str,
    campus_id: str,
    lits: Sequence[cp_model.IntVar],
    cache: dict[tuple[str, str], cp_model.IntVar],
) -> cp_model.IntVar:
    """"This session is on this campus" — one variable, however many pairs.

    Travel is pairwise, so an uncached indicator would be rebuilt once per
    partner: on a teacher with 20 sessions that is 190 copies of the same
    `AddMaxEquality` saying the same thing.
    """
    key = (session_id, campus_id)
    existing = cache.get(key)
    if existing is not None:
        return existing
    var = v.model.NewBoolVar(f"on[{session_id},{campus_id}]")
    v.model.AddMaxEquality(var, lits)
    cache[key] = var
    return var


def _post_travel(
    v: ModelVariables, pairs: set[tuple[str, str]], code: str
) -> AdapterResult:
    """Two sessions on different campuses need the journey between them.

    Scoped to a **single day**, under a reified ``same_day`` literal. Travel is
    a statement about elapsed time, and the occupancy index only measures
    elapsed time inside one day — across a day boundary it measures the stride.
    Posting the gap unconditionally therefore charges a teacher for a journey
    they make overnight, and since `TEACHER_CAMPUS_TRAVEL` is HARD and
    non-overridable, the result is a *proven* INFEASIBLE for somebody who
    simply works two campuses on two different days, with nobody able to
    override it. The validator has always day-scoped this; the builder now
    agrees with it.
    """
    posted = 0
    indicators: dict[tuple[str, str], cp_model.IntVar] = {}
    for a, b in sorted(pairs):
        campuses_a = _campus_literals(v, a)
        campuses_b = _campus_literals(v, b)
        if not campuses_a or not campuses_b:
            continue
        # Directional, as the catalog requires: A->B need not equal B->A, and
        # which campus is the origin depends on the start variables, so both
        # orientations are posted and the ordering literal picks one.
        gaps = {
            (ca, cb): (_travel_slots(v, ca, cb), _travel_slots(v, cb, ca))
            for ca in campuses_a
            for cb in campuses_b
        }
        # Cheapest test first. A single-campus institution — the common case,
        # and the whole of the FET sample — reaches no further than this, and
        # pays for no day variables at all.
        if not any(ab or ba for ab, ba in gaps.values()):
            continue

        same_day = v.model.NewBoolVar(f"{code}[{a}~{b}]")
        day_a, day_b = v.day_var(a), v.day_var(b)
        v.model.Add(day_a == day_b).OnlyEnforceIf(same_day)
        v.model.Add(day_a != day_b).OnlyEnforceIf(same_day.Not())

        dur_a = v.sessions[a].duration_slots
        dur_b = v.sessions[b].duration_slots
        for (ca, cb), (gap_ab, gap_ba) in sorted(gaps.items()):
            if not gap_ab and not gap_ba:
                continue
            on_a = _campus_indicator(v, a, ca, campuses_a[ca], indicators)
            on_b = _campus_indicator(v, b, cb, campuses_b[cb], indicators)
            a_first = v.model.NewBoolVar(f"{code}[{a}<{b},{ca},{cb}]")
            guard = [on_a, on_b, same_day]
            # Both orientations are posted even when one gap is zero. Skipping
            # the zero branch would leave `a_first` free to select it, and the
            # solver would take that exit and schedule the journey it cannot
            # make in the other direction. A zero gap still means "not at the
            # same time", which is exactly what the branch then says.
            v.model.Add(v.start[a] + dur_a + gap_ab <= v.start[b]).OnlyEnforceIf(
                [*guard, a_first]
            )
            v.model.Add(v.start[b] + dur_b + gap_ba <= v.start[a]).OnlyEnforceIf(
                [*guard, a_first.Not()]
            )
            posted += 1
    return AdapterResult(code, posted)


@_adapter("TEACHER_CAMPUS_TRAVEL")
def teacher_campus_travel(v: ModelVariables) -> AdapterResult:
    pairs: set[tuple[str, str]] = set()
    teachers = {t for sid in v.scheduled_ids() for t in v.sessions[sid].teacher_ids}
    for teacher_id in teachers:
        owned = v.sessions_of_teacher(teacher_id)
        for i, a in enumerate(owned):
            for b in owned[i + 1 :]:
                pairs.add(tuple(sorted((a, b))))  # type: ignore[arg-type]
    return _post_travel(v, pairs, "TEACHER_CAMPUS_TRAVEL")


@_adapter("STUDENT_CAMPUS_TRAVEL")
def student_campus_travel(v: ModelVariables) -> AdapterResult:
    by_student: dict[str, set[str]] = {}
    for enrollment in v.instance.snapshot.enrollments:
        by_student.setdefault(enrollment.student_id, set()).update(
            s for s in enrollment.session_ids if s in v.start
        )
    pairs: set[tuple[str, str]] = set()
    for sessions in by_student.values():
        ordered = sorted(sessions)
        for i, a in enumerate(ordered):
            for b in ordered[i + 1 :]:
                pairs.add((a, b))
    return _post_travel(v, pairs, "STUDENT_CAMPUS_TRAVEL")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def registered_codes() -> frozenset[str]:
    return frozenset(REGISTRY)


def apply_hard_constraints(v: ModelVariables) -> tuple[AdapterResult, ...]:
    """Post every HARD constraint this instance enforces.

    Only the codes in `instance.hard_codes()` — a constraint a named human
    downgraded is no longer posted, it is minimised in the objective instead,
    and posting it anyway would enforce something the institution explicitly
    decided to stop enforcing.
    """
    results = []
    for code in sorted(v.instance.hard_codes()):
        adapter = REGISTRY.get(code)
        if adapter is None:  # pragma: no cover - guarded by missing_adapters
            raise RuntimeError(
                f"no adapter for HARD constraint {code}; the solver must not "
                "run — an unenforced HARD constraint returns a confident "
                "OPTIMAL for an impossible schedule"
            )
        results.append(adapter(v))
    return tuple(results)
