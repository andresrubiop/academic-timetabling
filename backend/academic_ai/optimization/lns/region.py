"""Minimal-disruption re-optimisation: repair the region a scenario touched.

The question a what-if asks is not "what is the best timetable" — it is "what
is the smallest change that absorbs this". Those have very different answers.
Re-solving the period from scratch to accommodate one teacher's Tuesday
absence produces a schedule that satisfies the new constraint and moves two
hundred classes that had no reason to move, and every one of those moves is a
student's week rearranged and a room booking somebody already told people
about.

So the strategy is: free the affected region, keep everything else, and let
the objective's stability tier price any further movement.

Two policies, and the order matters
===================================

`AffectedRegionDestroy` frees exactly what the overlay touched. It runs first
because if the change absorbs locally, nothing else should move at all.

`RegionNeighbourhoodDestroy` widens by one ring — sessions that share a
teacher, a room or a section with the affected set — for the case where the
region alone is too tight. Widening is what makes a *feasible* repair possible
when the obvious one is not; it is deliberately a fallback rather than the
default, because a wider neighbourhood is a licence to move more.

Neither policy decides anything about feasibility. They choose which sessions
to free; CP-SAT decides what may replace them (CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from academic_ai.domain.scheduling.neighborhood import Neighborhood, SearchState

__all__ = ["AffectedRegionDestroy", "RegionNeighbourhoodDestroy", "region_policies"]


@dataclass(frozen=True, slots=True)
class AffectedRegionDestroy:
    """Free exactly the sessions the scenario touched.

    The tightest possible neighbourhood, and the one that produces the
    smallest diff when it works. Deterministic: the same overlay frees the
    same sessions every time, which is what makes a what-if reproducible.
    """

    session_ids: frozenset[str] = frozenset()
    code: str = "AFFECTED_REGION"

    def select(self, state: SearchState) -> Neighborhood:
        freed = frozenset(
            sid for sid in self.session_ids if sid in state.placement
        )
        return Neighborhood(
            freed,
            self.code,
            f"{len(freed)} session(s) the scenario touched directly",
        )


@dataclass(frozen=True, slots=True)
class RegionNeighbourhoodDestroy:
    """The affected region plus one ring of sessions that share a resource.

    A teacher who becomes unavailable on Tuesday cannot always be absorbed by
    moving only their own classes: the slot they need may be held by a
    colleague in the same room. Widening by one ring gives the solver
    somewhere to put things without opening the whole timetable.

    One ring, not two, and not transitively. Each ring roughly squares the
    neighbourhood on a dense timetable, and a neighbourhood that reaches
    everything is just a full re-solve with extra steps.
    """

    session_ids: frozenset[str] = frozenset()
    #: Cap, so a hub teacher with forty sessions cannot drag in the period.
    max_size: int = 60
    code: str = "AFFECTED_REGION_RING"

    def select(self, state: SearchState) -> Neighborhood:
        sessions = {s.id: s for s in state.instance.snapshot.sessions}
        seeds = {sid for sid in self.session_ids if sid in state.placement}

        teachers: set[str] = set()
        sections: set[str] = set()
        rooms: set[str] = set()
        for sid in seeds:
            spec = sessions.get(sid)
            if spec is None:
                continue
            teachers.update(spec.teacher_ids)
            sections.add(spec.section_id)
            placed = state.placement.get(sid)
            if placed is not None and placed.room_id:
                rooms.add(placed.room_id)

        ring: set[str] = set()
        for sid, placed in state.placement.items():
            if sid in seeds:
                continue
            spec = sessions.get(sid)
            if spec is None:
                continue
            if (
                set(spec.teacher_ids) & teachers
                or spec.section_id in sections
                or (placed.room_id and placed.room_id in rooms)
            ):
                ring.add(sid)

        # Seeds first, then as much of the ring as the cap allows. Sorted, so
        # a truncated ring is the same truncated ring on every run --
        # SESSION 4 forbids claiming an improvement without reproducible
        # evidence, and a nondeterministic neighbourhood breaks that.
        budget = max(0, self.max_size - len(seeds))
        freed = seeds | set(sorted(ring)[:budget])
        return Neighborhood(
            frozenset(freed),
            self.code,
            f"{len(seeds)} affected session(s) plus {len(freed) - len(seeds)} "
            "sharing a teacher, room or section",
        )


def region_policies(
    session_ids: frozenset[str], *, max_size: int = 60
) -> tuple[object, ...]:
    """The two policies, tightest first.

    Order is the strategy: try to absorb the change where it happened, and
    only widen when that fails. Handing the engine the wide policy first would
    move sessions that the narrow one would have left alone.
    """
    return (
        AffectedRegionDestroy(session_ids=session_ids),
        RegionNeighbourhoodDestroy(session_ids=session_ids, max_size=max_size),
    )
