"""`ScheduleDiff`: what changed between two schedules, and who it reached.

The report a coordinator reads after a what-if, and the same one SESSION 12
will attach to a change request. Built on `impact.py` rather than beside it:
an impact preview and a scenario diff answer the same question at different
scales, and two implementations of "who does this affect" would eventually
disagree — with the disagreement showing up as a preview that promised one
thing and a diff that reported another.

What it deliberately does not say
=================================

It does not say whether the result is feasible. CP-SAT decides that, and a
diff computed here is a description of two placements, not a verdict on
either.

It does not collapse the score into one number. A single figure would
reintroduce the cross-tier exchange rate ADR-0010 rejects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from academic_ai.domain.scheduling.impact import (
    AffectedCounts,
    ConflictChange,
    ImpactReport,
    ProposedMove,
    analyse_impact,
)
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.domain.scheduling.problem import ProblemInstance

__all__ = ["MovedSession", "ScheduleDiff", "diff_schedules"]


@dataclass(frozen=True, slots=True)
class MovedSession:
    """One session that ended up somewhere else."""

    session_id: str
    from_start_slot_id: str | None
    to_start_slot_id: str | None
    from_room_id: str | None
    to_room_id: str | None

    @property
    def changed_time(self) -> bool:
        return self.from_start_slot_id != self.to_start_slot_id

    @property
    def changed_room(self) -> bool:
        return self.from_room_id != self.to_room_id

    @property
    def is_new(self) -> bool:
        """Appeared in the new schedule -- a NEW_SECTION, typically."""
        return self.from_start_slot_id is None

    @property
    def is_dropped(self) -> bool:
        """Present before and unplaced after. Worth its own name: a session
        that vanished is a much louder fact than one that moved."""
        return self.to_start_slot_id is None

    def describe(self) -> str:
        if self.is_new:
            return f"{self.session_id}: newly placed at {self.to_start_slot_id}"
        if self.is_dropped:
            return f"{self.session_id}: no longer placed"
        parts = []
        if self.changed_time:
            parts.append(f"{self.from_start_slot_id} -> {self.to_start_slot_id}")
        if self.changed_room:
            parts.append(f"room {self.from_room_id} -> {self.to_room_id}")
        return f"{self.session_id}: " + ", ".join(parts)


@dataclass(frozen=True, slots=True)
class ScheduleDiff:
    """Everything the playbook asks a what-if to report."""

    moved: tuple[MovedSession, ...] = ()
    unchanged: tuple[str, ...] = ()
    affected: AffectedCounts = field(default_factory=AffectedCounts)
    new_conflicts: tuple[ConflictChange, ...] = ()
    resolved_conflicts: tuple[ConflictChange, ...] = ()
    score_delta: tuple[int, ...] = ()
    tier_codes: tuple[str, ...] = ()
    stability_delta: int = 0
    occupancy_clashes: tuple[str, ...] = ()

    @property
    def moved_count(self) -> int:
        return len(self.moved)

    @property
    def unchanged_count(self) -> int:
        return len(self.unchanged)

    @property
    def is_improvement(self) -> bool:
        """Lexicographic. The first tier that moves decides."""
        for delta in self.score_delta:
            if delta != 0:
                return delta < 0
        return False

    @property
    def is_noop(self) -> bool:
        return not self.moved and not self.new_conflicts and not self.resolved_conflicts

    def named_delta(self) -> Mapping[str, int]:
        return dict(zip(self.tier_codes, self.score_delta))

    def summary(self) -> str:
        """One line. What a coordinator reads before deciding to read more."""
        if self.is_noop:
            return "nothing changed"
        bits = [f"{self.moved_count} session(s) moved", f"{self.unchanged_count} unchanged"]
        if self.new_conflicts:
            bits.append(f"{len(self.new_conflicts)} new conflict(s)")
        if self.resolved_conflicts:
            bits.append(f"{len(self.resolved_conflicts)} resolved")
        return "; ".join(bits)


def _as_mapping(
    placement: Mapping[str, PlacedSession] | Sequence[PlacedSession],
) -> dict[str, PlacedSession]:
    if isinstance(placement, Mapping):
        return dict(placement)
    return {p.session_id: p for p in placement}


def diff_schedules(
    instance: ProblemInstance,
    before: Mapping[str, PlacedSession] | Sequence[PlacedSession],
    after: Mapping[str, PlacedSession] | Sequence[PlacedSession],
) -> ScheduleDiff:
    """Compare two placements of one instance.

    ``instance`` is the one the *after* schedule belongs to, because that is
    the world the conflicts and the objective are measured in. Passing the
    before-instance would score the new schedule against the old constraints
    and report a delta that describes neither.
    """
    before_map = _as_mapping(before)
    after_map = _as_mapping(after)

    moved: list[MovedSession] = []
    unchanged: list[str] = []

    for session_id in sorted(set(before_map) | set(after_map)):
        old = before_map.get(session_id)
        new = after_map.get(session_id)
        if old is not None and new is not None:
            if old.start_slot_id == new.start_slot_id and old.room_id == new.room_id:
                unchanged.append(session_id)
                continue
        moved.append(
            MovedSession(
                session_id=session_id,
                from_start_slot_id=old.start_slot_id if old else None,
                to_start_slot_id=new.start_slot_id if new else None,
                from_room_id=old.room_id if old else None,
                to_room_id=new.room_id if new else None,
            )
        )

    # Reuse the impact engine for everything that is a question about people
    # and the objective. Expressing the difference as moves lets one
    # implementation serve both the SESSION 10 preview and this diff.
    as_moves = [
        ProposedMove(
            session_id=m.session_id,
            to_start_slot_id=m.to_start_slot_id,
            to_room_id=m.to_room_id,
        )
        for m in moved
        if m.to_start_slot_id is not None
    ]
    # Sessions absent from `before` cannot be "moved" from it, so seed them in
    # at their new position; otherwise a NEW_SECTION reports as invisible.
    seeded = dict(before_map)
    for m in moved:
        if m.is_new and m.to_start_slot_id is not None:
            seeded[m.session_id] = PlacedSession(
                m.session_id, m.to_start_slot_id, m.to_room_id
            )

    impact: ImpactReport = analyse_impact(instance, seeded, as_moves)

    return ScheduleDiff(
        moved=tuple(moved),
        unchanged=tuple(unchanged),
        affected=impact.affected,
        new_conflicts=impact.new_conflicts,
        resolved_conflicts=impact.resolved_conflicts,
        score_delta=impact.score_delta,
        tier_codes=impact.tier_codes,
        stability_delta=impact.stability_delta,
        occupancy_clashes=impact.occupancy_clashes,
    )
