"""Destroy strategies: the eight heuristic `NeighborhoodPolicy` implementations.

Each answers the same question differently — *which part of this schedule is
worth re-deciding?* — and each is deliberately simple enough to explain to a
coordinator, because SESSION 6 has to turn these into recommendations and
SESSION 4 has to justify that they helped.

They are heuristics, and the point of the shared contract is that a learned
policy (SESSION 7 onward) plugs in beside them and is benchmarked against
them. Nothing here imports OR-Tools: a policy selects session ids, and CP-SAT
remains the only thing that decides whether the result is feasible.

Determinism is a hard requirement, not a nicety. Every policy derives its
randomness from `SearchState.seed`, so two runs of the same benchmark produce
the same neighbourhoods. SESSION 4 forbids claiming an improvement without
reproducible evidence, and a policy that consulted a global RNG would make
every such claim unfalsifiable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from academic_ai.domain.scheduling.evaluate import occupied_cells
from academic_ai.domain.scheduling.neighborhood import (
    Neighborhood,
    NeighborhoodPolicy,
    SearchState,
)
from academic_ai.domain.scheduling.problem import classify_student_conflict
from academic_ai.domain.scheduling.priority import criticality, enrollment_key

__all__ = [
    "DEFAULT_POLICIES",
    "POLICY_REGISTRY",
    "ConflictComponentDestroy",
    "HighConflictDestroy",
    "ProgramDestroy",
    "RandomDestroy",
    "RoomBottleneckDestroy",
    "StudentCriticalityDestroy",
    "TeacherBottleneckDestroy",
    "TimeWindowDestroy",
    "policy_by_code",
]


def _rng(state: SearchState, salt: str) -> Callable[[int], int]:
    """A deterministic index generator seeded by state and policy.

    Hashing rather than `random.Random` so the sequence depends only on values
    we control — a benchmark replayed on another machine, or after an
    unrelated import reorders module loading, must produce the same
    neighbourhoods.
    """
    counter = [0]

    def below(bound: int) -> int:
        if bound <= 0:
            return 0
        payload = f"{salt}|{state.seed}|{state.iteration}|{counter[0]}".encode()
        counter[0] += 1
        return int.from_bytes(hashlib.blake2b(payload, digest_size=8).digest()) % bound

    return below


def _sample(items: Sequence[str], count: int, below: Callable[[int], int]) -> set[str]:
    """`count` distinct items, chosen deterministically."""
    pool = list(items)
    chosen: set[str] = set()
    while pool and len(chosen) < count:
        chosen.add(pool.pop(below(len(pool))))
    return chosen


def _target_size(state: SearchState, fraction: float, floor: int = 2) -> int:
    total = len(state.placement)
    return max(floor, min(total, int(total * fraction)))


@dataclass(frozen=True, slots=True)
class RandomDestroy:
    """Free a random slice.

    The baseline every other policy has to beat. A targeted strategy that
    cannot outperform random selection is not targeting anything, and without
    this in the comparison there would be no way to tell.
    """

    fraction: float = 0.15
    code: str = "RANDOM"

    def select(self, state: SearchState) -> Neighborhood:
        below = _rng(state, self.code)
        size = _target_size(state, self.fraction)
        chosen = _sample(state.scheduled_ids(), size, below)
        return Neighborhood(
            frozenset(chosen),
            self.code,
            f"{len(chosen)} sessions chosen at random",
        )


@dataclass(frozen=True, slots=True)
class HighConflictDestroy:
    """Free the sessions currently involved in student clashes.

    The most direct attack on the tier that usually dominates: if a mandatory
    conflict is what the score is made of, the sessions causing it are the
    ones worth re-deciding.
    """

    fraction: float = 0.15
    code: str = "HIGH_CONFLICT"

    def select(self, state: SearchState) -> Neighborhood:
        cells = {
            sid: occupied_cells(state.instance, p) for sid, p in state.placement.items()
        }
        weight: dict[str, int] = {}
        by_student: dict[str, list] = {}
        for enrollment in state.instance.snapshot.enrollments:
            by_student.setdefault(enrollment.student_id, []).append(enrollment)

        for enrollments in by_student.values():
            for i, a in enumerate(enrollments):
                for b in enrollments[i + 1 :]:
                    if not classify_student_conflict(a, b).startswith(
                        "STUDENT_NO_OVERLAP"
                    ):
                        continue
                    for sa in a.session_ids:
                        for sb in b.session_ids:
                            if sa == sb or sa not in cells or sb not in cells:
                                continue
                            if cells[sa] & cells[sb]:
                                weight[sa] = weight.get(sa, 0) + 1
                                weight[sb] = weight.get(sb, 0) + 1

        size = _target_size(state, self.fraction)
        ranked = sorted(weight, key=lambda s: (-weight[s], s))[:size]
        if len(ranked) < size:
            below = _rng(state, self.code)
            ranked += sorted(
                _sample(
                    [s for s in state.scheduled_ids() if s not in ranked],
                    size - len(ranked),
                    below,
                )
            )
        return Neighborhood(
            frozenset(ranked),
            self.code,
            f"{len(ranked)} sessions ranked by student-clash incidence",
            {"conflicted_sessions": len(weight)},
        )


@dataclass(frozen=True, slots=True)
class StudentCriticalityDestroy:
    """Free the sessions of the students with the least room to manoeuvre.

    Weighted by how stuck the student is: an enrollment with no alternative
    section and no alternative offering counts far more than one with three
    of each. This is the LNS face of the rule the platform exists to enforce —
    one student who cannot move outranks many who can.

    The weighting comes from `state.priorities`, i.e. from whatever
    `ConflictPriorityProvider` the run is using, falling back to
    `criticality()` — the reciprocal of the student's options, doubled for
    mandatory and again per repeat attempt. Reading `criticality()`
    unconditionally would rank students by the heuristic while the objective
    ranked them by an active model, and the search would spend its budget
    freeing sessions the objective was not scoring.

    Additive over students by construction, which is what lets the CP-SAT
    encoding aggregate a session pair into one weighted literal.
    """

    fraction: float = 0.15
    code: str = "STUDENT_CRITICALITY"

    def select(self, state: SearchState) -> Neighborhood:
        weight: dict[str, float] = {}
        for enrollment in state.instance.snapshot.enrollments:
            score = float(
                state.priorities.get(
                    enrollment_key(enrollment), criticality(enrollment)
                )
            )
            for session_id in enrollment.session_ids:
                if session_id in state.placement:
                    weight[session_id] = weight.get(session_id, 0.0) + score

        size = _target_size(state, self.fraction)
        ranked = sorted(weight, key=lambda s: (-weight[s], s))[:size]
        return Neighborhood(
            frozenset(ranked),
            self.code,
            f"{len(ranked)} sessions of the least-flexible students",
        )


@dataclass(frozen=True, slots=True)
class TeacherBottleneckDestroy:
    """Free everything belonging to the busiest teachers.

    A teacher near their limit constrains every session they touch, so the
    sessions worth re-deciding together are theirs — freeing half of them
    leaves the disjunction just as tight.
    """

    teachers: int = 2
    code: str = "TEACHER_BOTTLENECK"

    def select(self, state: SearchState) -> Neighborhood:
        sessions = {s.id: s for s in state.instance.snapshot.sessions}
        load: dict[str, int] = {}
        for session_id in state.placement:
            for teacher_id in sessions[session_id].teacher_ids:
                load[teacher_id] = load.get(teacher_id, 0) + sessions[
                    session_id
                ].duration_slots

        busiest = sorted(load, key=lambda t: (-load[t], t))[: self.teachers]
        chosen = {
            session_id
            for session_id in state.placement
            if set(sessions[session_id].teacher_ids) & set(busiest)
        }
        return Neighborhood(
            frozenset(chosen),
            self.code,
            f"all {len(chosen)} sessions of the {len(busiest)} busiest teacher(s)",
            {"teachers": busiest},
        )


@dataclass(frozen=True, slots=True)
class RoomBottleneckDestroy:
    """Free everything competing for the most contended rooms."""

    rooms: int = 2
    code: str = "ROOM_BOTTLENECK"

    def select(self, state: SearchState) -> Neighborhood:
        load: dict[str, int] = {}
        for placed in state.placement.values():
            if placed.room_id:
                load[placed.room_id] = load.get(placed.room_id, 0) + 1

        busiest = sorted(load, key=lambda r: (-load[r], r))[: self.rooms]
        chosen = {
            sid
            for sid, placed in state.placement.items()
            if placed.room_id in busiest
        }
        return Neighborhood(
            frozenset(chosen),
            self.code,
            f"all {len(chosen)} sessions in the {len(busiest)} busiest room(s)",
            {"rooms": busiest},
        )


@dataclass(frozen=True, slots=True)
class TimeWindowDestroy:
    """Free one contiguous band of the week.

    Re-deciding a whole morning at once lets sessions genuinely swap places,
    which a scattered selection rarely allows: two classes can only exchange
    slots if both are free at the same moment.
    """

    span_slots: int = 3
    code: str = "TIME_WINDOW"

    def select(self, state: SearchState) -> Neighborhood:
        slots = {s.id: s for s in state.instance.snapshot.slots}
        days = [d.value for d in state.instance.snapshot.period.days]
        if not days:
            return Neighborhood(frozenset(), self.code, "no days in the grid")

        below = _rng(state, self.code)
        day = days[below(len(days))]
        starts = sorted(
            {s.in_day_index for s in slots.values() if s.day_of_week.value == day}
        )
        if not starts:
            return Neighborhood(frozenset(), self.code, f"no slots on {day}")
        window_start = starts[below(len(starts))]
        window = range(window_start, window_start + self.span_slots)

        chosen = {
            sid
            for sid, placed in state.placement.items()
            if (slot := slots.get(placed.start_slot_id))
            and slot.day_of_week.value == day
            and slot.in_day_index in window
        }
        return Neighborhood(
            frozenset(chosen),
            self.code,
            f"{len(chosen)} sessions in {day} slots {window_start}"
            f"-{window_start + self.span_slots - 1}",
            {"day": day, "from_index": window_start, "span": self.span_slots},
        )


@dataclass(frozen=True, slots=True)
class ProgramDestroy:
    """Free one programme's sessions.

    The unit a coordinator actually owns. A neighbourhood aligned to it
    produces changes one person can approve, which matters once SESSION 12
    routes these through an approval workflow.
    """

    code: str = "PROGRAM"

    def select(self, state: SearchState) -> Neighborhood:
        sessions = {s.id: s for s in state.instance.snapshot.sessions}
        by_offering: dict[str, set[str]] = {}
        for session_id in state.placement:
            by_offering.setdefault(sessions[session_id].offering_id, set()).add(
                session_id
            )
        if not by_offering:
            return Neighborhood(frozenset(), self.code, "nothing placed")

        below = _rng(state, self.code)
        keys = sorted(by_offering)
        chosen_key = keys[below(len(keys))]
        return Neighborhood(
            frozenset(by_offering[chosen_key]),
            self.code,
            f"{len(by_offering[chosen_key])} sessions of offering {chosen_key}",
            {"offering_id": chosen_key},
        )


@dataclass(frozen=True, slots=True)
class ConflictComponentDestroy:
    """Free one connected component of the student conflict graph.

    Sessions that share students form components; a clash can only be resolved
    by moving sessions within one. Freeing a whole component is therefore the
    smallest selection that can actually fix it, and freeing across components
    is wasted freedom.

    This is the heuristic ancestor of SESSION 5's Student Conflict Graph, and
    a natural place for SESSION 7's GNN to take over.
    """

    max_component: int = 40
    code: str = "CONFLICT_COMPONENT"

    def select(self, state: SearchState) -> Neighborhood:
        adjacency: dict[str, set[str]] = {sid: set() for sid in state.placement}
        by_student: dict[str, set[str]] = {}
        for enrollment in state.instance.snapshot.enrollments:
            by_student.setdefault(enrollment.student_id, set()).update(
                s for s in enrollment.session_ids if s in state.placement
            )
        for shared in by_student.values():
            ordered = sorted(shared)
            for i, a in enumerate(ordered):
                for b in ordered[i + 1 :]:
                    adjacency[a].add(b)
                    adjacency[b].add(a)

        below = _rng(state, self.code)
        roots = sorted(adjacency)
        if not roots:
            return Neighborhood(frozenset(), self.code, "nothing placed")

        root = roots[below(len(roots))]
        component: set[str] = set()
        frontier = [root]
        while frontier and len(component) < self.max_component:
            current = frontier.pop()
            if current in component:
                continue
            component.add(current)
            frontier.extend(sorted(adjacency[current] - component))

        return Neighborhood(
            frozenset(component),
            self.code,
            f"{len(component)} sessions from one conflict-graph component",
            {"root": root},
        )


#: The eight strategies the playbook names for SESSION 4.
DEFAULT_POLICIES: tuple[NeighborhoodPolicy, ...] = (
    RandomDestroy(),
    HighConflictDestroy(),
    StudentCriticalityDestroy(),
    TeacherBottleneckDestroy(),
    RoomBottleneckDestroy(),
    TimeWindowDestroy(),
    ProgramDestroy(),
    ConflictComponentDestroy(),
)

POLICY_REGISTRY: Mapping[str, NeighborhoodPolicy] = {
    policy.code: policy for policy in DEFAULT_POLICIES
}


def policy_by_code(code: str) -> NeighborhoodPolicy:
    try:
        return POLICY_REGISTRY[code]
    except KeyError:
        raise KeyError(
            f"unknown policy {code!r}; known: {', '.join(sorted(POLICY_REGISTRY))}"
        ) from None
