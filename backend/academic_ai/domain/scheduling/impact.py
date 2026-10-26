"""What a proposed move would do, before anybody does it (SESSION 10).

The engine behind the preview ADR-0012 requires. A drag produces a proposal;
this turns the proposal into the numbers a coordinator needs to decide, and it
does so **without touching anything**: it takes a placement, returns a report,
and has no way to write.

Why the numbers here and not a "looks fine" badge
=================================================

The preview is the only thing standing between a slipped hand and a change
request. If it said nothing more than "this is allowed", the interaction would
be a speed bump and people would learn to click through it. So it reports what
a coordinator could not have worked out by looking: who is affected, which
clashes appear and disappear, and what happens to each objective tier.

Framework-free, and solver-free. It measures a *given* placement rather than
searching for one, so it runs in the milliseconds a drag can afford. It
deliberately cannot say whether the result is feasible — that is CP-SAT's, and
a second opinion computed here would drift from it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from academic_ai.domain.scheduling.evaluate import ScoreVector, evaluate, occupied_cells
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.domain.scheduling.priority import (
    PriorityRequest,
    conflict_pair_weights,
    resolve_priorities,
)
from academic_ai.domain.scheduling.problem import (
    ProblemInstance,
    classify_student_conflict,
)

__all__ = [
    "AffectedCounts",
    "ConflictChange",
    "ImpactReport",
    "ProposedMove",
    "analyse_impact",
    "apply_moves",
]


@dataclass(frozen=True, slots=True)
class ProposedMove:
    """One session, moved. The unit a drag produces.

    ``to_room_id`` of ``None`` means "leave the room alone", which is not the
    same as "no room": a drag along the time axis must not silently unassign a
    room.
    """

    session_id: str
    to_start_slot_id: str
    to_room_id: str | None = None


@dataclass(frozen=True, slots=True)
class AffectedCounts:
    """Who this touches. Counts, because the preview is read at a glance."""

    students: int = 0
    teachers: int = 0
    programs: int = 0
    faculties: int = 0
    rooms: int = 0
    sessions: int = 0

    @property
    def is_empty(self) -> bool:
        return not any(
            (self.students, self.teachers, self.programs, self.faculties,
             self.rooms, self.sessions)
        )


@dataclass(frozen=True, slots=True)
class ConflictChange:
    """One student clash that appears or disappears.

    Named, not counted. "3 new conflicts" tells a coordinator to investigate;
    "Ana Pérez can no longer attend Cálculo II" tells them what to do.
    """

    student_id: str
    session_a: str
    session_b: str
    classification: str

    @property
    def is_unresolvable(self) -> bool:
        return self.classification.endswith("UNRESOLVABLE")


@dataclass(frozen=True, slots=True)
class ImpactReport:
    """Everything the preview shows. Produced without writing anything."""

    moves: tuple[ProposedMove, ...]
    affected: AffectedCounts
    new_conflicts: tuple[ConflictChange, ...] = ()
    resolved_conflicts: tuple[ConflictChange, ...] = ()
    #: Per tier, best-first, matching `ScoreVector`. Negative is better. Never
    #: collapsed to one number: a scalar reintroduces the cross-tier exchange
    #: rate ADR-0010 rejects, and "+3" for a move that strands one student
    #: while pleasing forty is worse than no number at all.
    score_delta: tuple[int, ...] = ()
    tier_codes: tuple[str, ...] = ()
    #: How many sessions end up somewhere other than the base version had
    #: them. The playbook's "stability delta".
    stability_delta: int = 0
    #: Sessions whose new position collides with something. Not a feasibility
    #: verdict -- CP-SAT owns that -- but enough to warn before submitting.
    occupancy_clashes: tuple[str, ...] = ()
    unevaluated_tiers: frozenset[str] = frozenset()
    #: *Which* programmes this change reaches, not just how many.
    #:
    #: `affected` carries counts because a preview is read at a glance, and
    #: those are what `assess_scope` routes on. Approval routing needs the ids
    #: as well: "every coordinator must sign" and "the coordinators of these
    #: three programmes must sign" are different questions, and only the
    #: second can be answered from a count of three.
    affected_program_ids: tuple[str, ...] = ()
    affected_faculty_ids: tuple[str, ...] = ()

    @property
    def is_improvement(self) -> bool:
        """Lexicographic: the first tier that moves decides."""
        for delta in self.score_delta:
            if delta != 0:
                return delta < 0
        return False

    @property
    def is_neutral(self) -> bool:
        return all(d == 0 for d in self.score_delta) and not self.new_conflicts

    def named_delta(self) -> Mapping[str, int]:
        return dict(zip(self.tier_codes, self.score_delta))

    def summary(self) -> str:
        """One line, for a UI that has not rendered the detail yet."""
        if self.new_conflicts:
            worst = "unresolvable " if any(c.is_unresolvable for c in self.new_conflicts) else ""
            return (
                f"creates {len(self.new_conflicts)} new {worst}student "
                f"conflict(s)"
            )
        if self.resolved_conflicts:
            return f"resolves {len(self.resolved_conflicts)} student conflict(s)"
        if self.is_neutral:
            return "no measured effect on students or the objective"
        return "changes the objective without changing student conflicts"


def apply_moves(
    placement: Mapping[str, PlacedSession], moves: Sequence[ProposedMove]
) -> dict[str, PlacedSession]:
    """A **copy** with the moves applied.

    Returns a new mapping rather than mutating. The caller holds the accepted
    placement, and a function that edited it in place would make "the drag did
    not change anything" depend on nobody having kept a reference.
    """
    updated = dict(placement)
    for move in moves:
        current = updated.get(move.session_id)
        if current is None:
            continue
        updated[move.session_id] = PlacedSession(
            session_id=move.session_id,
            start_slot_id=move.to_start_slot_id,
            # None means "leave the room alone" -- a drag along the time axis
            # must not silently unassign a room.
            room_id=move.to_room_id if move.to_room_id is not None else current.room_id,
        )
    return updated


def _student_conflicts(
    instance: ProblemInstance, placement: Mapping[str, PlacedSession]
) -> set[ConflictChange]:
    """Every (student, pair) clash present in this placement."""
    cells = {sid: occupied_cells(instance, p) for sid, p in placement.items()}
    by_student: dict[str, list] = {}
    for enrollment in instance.snapshot.enrollments:
        by_student.setdefault(enrollment.student_id, []).append(enrollment)

    found: set[ConflictChange] = set()
    for student_id, enrollments in by_student.items():
        for i, a in enumerate(enrollments):
            for b in enrollments[i + 1 :]:
                classification = classify_student_conflict(a, b)
                if not classification.startswith("STUDENT_NO_OVERLAP"):
                    continue
                for sa in a.session_ids:
                    for sb in b.session_ids:
                        if sa == sb or sa not in cells or sb not in cells:
                            continue
                        if cells[sa] & cells[sb]:
                            found.add(
                                ConflictChange(
                                    student_id=student_id,
                                    session_a=min(sa, sb),
                                    session_b=max(sa, sb),
                                    classification=classification,
                                )
                            )
    return found


def _occupancy_clashes(
    instance: ProblemInstance, placement: Mapping[str, PlacedSession]
) -> tuple[str, ...]:
    """Sessions sharing a room, a teacher or a section with an overlapping one.

    A warning, not a verdict. CP-SAT decides feasibility; this exists so the
    preview can say "this puts two classes in Room A at once" instead of
    letting a coordinator find out after the change request is approved.
    """
    cells = {sid: occupied_cells(instance, p) for sid, p in placement.items()}
    sessions = {s.id: s for s in instance.snapshot.sessions}
    clashing: set[str] = set()

    ids = sorted(cells)
    for i, sa in enumerate(ids):
        for sb in ids[i + 1 :]:
            if not (cells[sa] & cells[sb]):
                continue
            spec_a, spec_b = sessions.get(sa), sessions.get(sb)
            if spec_a is None or spec_b is None:
                continue
            shares_teacher = bool(set(spec_a.teacher_ids) & set(spec_b.teacher_ids))
            shares_section = spec_a.section_id == spec_b.section_id
            room_a = placement[sa].room_id
            room_b = placement[sb].room_id
            shares_room = room_a is not None and room_a == room_b
            if shares_teacher or shares_section or shares_room:
                clashing.add(sa)
                clashing.add(sb)
    return tuple(sorted(clashing))


def analyse_impact(
    instance: ProblemInstance,
    placement: Mapping[str, PlacedSession] | Sequence[PlacedSession],
    moves: Sequence[ProposedMove],
) -> ImpactReport:
    """What these moves would do to this schedule. Writes nothing.

    ``placement`` is the accepted schedule; it is never modified. The moved
    copy exists only for the length of this call, which is what makes the
    CRITICAL rule in ADR-0012 a property of the code rather than a promise.
    """
    if not isinstance(placement, Mapping):
        placement = {p.session_id: p for p in placement}

    moved = apply_moves(placement, moves)
    moved_ids = {m.session_id for m in moves if m.session_id in placement}

    priorities = resolve_priorities(PriorityRequest(instance))
    before_score = evaluate(instance, placement, priorities=priorities)
    after_score = evaluate(instance, moved, priorities=priorities)

    before_conflicts = _student_conflicts(instance, placement)
    after_conflicts = _student_conflicts(instance, moved)

    new_conflicts = tuple(sorted(after_conflicts - before_conflicts, key=_conflict_key))
    resolved = tuple(sorted(before_conflicts - after_conflicts, key=_conflict_key))

    affected, affected_programs, affected_faculties = _count_affected(
        instance, moved_ids, new_conflicts, resolved, moved
    )

    base = {b.session_id: b for b in instance.snapshot.base_assignments}
    stability = 0
    for session_id, placed in moved.items():
        anchor = base.get(session_id)
        if anchor is None:
            continue
        if placed.start_slot_id != anchor.start_slot_id or (
            anchor.room_id is not None and placed.room_id != anchor.room_id
        ):
            stability += 1
    base_stability = sum(
        1
        for session_id, placed in placement.items()
        if (anchor := base.get(session_id)) is not None
        and (
            placed.start_slot_id != anchor.start_slot_id
            or (anchor.room_id is not None and placed.room_id != anchor.room_id)
        )
    )

    return ImpactReport(
        moves=tuple(moves),
        affected=affected,
        new_conflicts=new_conflicts,
        resolved_conflicts=resolved,
        score_delta=_delta(before_score, after_score),
        tier_codes=after_score.tier_codes,
        stability_delta=stability - base_stability,
        occupancy_clashes=_occupancy_clashes(instance, moved),
        unevaluated_tiers=after_score.unevaluated | before_score.unevaluated,
        affected_program_ids=affected_programs,
        affected_faculty_ids=affected_faculties,
    )


def _conflict_key(change: ConflictChange) -> tuple[str, str, str]:
    return (change.student_id, change.session_a, change.session_b)


def _delta(before: ScoreVector, after: ScoreVector) -> tuple[int, ...]:
    if before.tier_codes != after.tier_codes:  # pragma: no cover - same instance
        raise ValueError("cannot compare score vectors from different profiles")
    return tuple(a - b for a, b in zip(after.values, before.values))


def _count_affected(
    instance: ProblemInstance,
    moved_ids: set[str],
    new_conflicts: Sequence[ConflictChange],
    resolved: Sequence[ConflictChange],
    moved_placement: Mapping[str, PlacedSession],
) -> tuple[AffectedCounts, tuple[str, ...], tuple[str, ...]]:
    """Who is touched: the moved sessions' people, plus anybody whose clashes
    changed.

    A student enrolled in a moved session is affected even when nothing broke
    -- their week changed -- which is why this is not simply the conflict
    count.
    """
    sessions = {s.id: s for s in instance.snapshot.sessions}

    teachers: set[str] = set()
    programs: set[str] = set()
    faculties: set[str] = set()
    rooms: set[str] = set()
    students: set[str] = set()

    for session_id in moved_ids:
        spec = sessions.get(session_id)
        if spec is None:
            continue
        teachers.update(spec.teacher_ids)
        placed = moved_placement.get(session_id)
        if placed is not None and placed.room_id:
            rooms.add(placed.room_id)
        # Read off the session, which carries its offering's programmes.
        #
        # This used to look them up in `snapshot.offerings` via `getattr(...,
        # ())` -- a field `BaseSnapshot` has never had. The fallback made the
        # loop a no-op, so `programs` and `faculties` were **always 0**, and
        # `assess_scope` therefore never saw a cross-programme or
        # cross-faculty change: every request routed to the narrowest chain.
        # The tests missed it because they build `AffectedCounts` by hand.
        programs.update(spec.program_ids)
        faculties.update(spec.faculty_ids)

    for enrollment in instance.snapshot.enrollments:
        if moved_ids & set(enrollment.session_ids):
            students.add(enrollment.student_id)

    students.update(c.student_id for c in new_conflicts)
    students.update(c.student_id for c in resolved)

    return (
        AffectedCounts(
            students=len(students),
            teachers=len(teachers),
            programs=len(programs),
            faculties=len(faculties),
            rooms=len(rooms),
            sessions=len(moved_ids),
        ),
        tuple(sorted(programs)),
        tuple(sorted(faculties)),
    )
