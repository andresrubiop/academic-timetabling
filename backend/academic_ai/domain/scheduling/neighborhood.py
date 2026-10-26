"""`NeighborhoodPolicy` — which part of a schedule a repair may move.

This is one of the three AI contracts named in ADR-0001, so it lives in the
framework-free domain and not beside the solver. A policy answers a purely
domain question — *given this schedule, which sessions should be freed?* — and
answers it with session ids. Nothing here imports OR-Tools, which is what lets
SESSION 7's GNN implement the same protocol without the solver in scope, and
what lets a heuristic and a learned policy be swapped without either knowing
the other exists.

The contract is deliberately narrow. A policy **selects**; it does not repair,
does not score, and cannot place anything. That boundary is what keeps
CLAUDE.md's rule enforceable: AI guides the search, and CP-SAT remains the
feasibility authority. A policy that returned assignments rather than a
selection would be deciding feasibility, and no amount of review would make
that safe again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, runtime_checkable

from academic_ai.domain.scheduling.problem import ProblemInstance

__all__ = [
    "Neighborhood",
    "NeighborhoodPolicy",
    "PlacedSession",
    "SearchState",
]


@dataclass(frozen=True, slots=True)
class PlacedSession:
    """One session as currently scheduled."""

    session_id: str
    start_slot_id: str
    room_id: str | None = None


@dataclass(frozen=True, slots=True)
class SearchState:
    """Everything a policy is allowed to see.

    Deliberately a snapshot rather than a live handle on the search: a policy
    must not be able to reach into the solver, and a policy that behaved
    differently depending on solver internals could not be reproduced — which
    SESSION 4 forbids, since no improvement may be claimed without
    reproducible evidence.
    """

    instance: ProblemInstance
    #: The incumbent schedule, by session id.
    placement: Mapping[str, PlacedSession]
    iteration: int = 0
    #: Deterministic per iteration, so a policy that randomises is replayable.
    seed: int = 0
    #: Tier values of the incumbent, best-first. Lets a policy target the tier
    #: that is actually hurting rather than guessing.
    score: tuple[int, ...] = ()
    #: Sessions freed on recent iterations, so a policy can avoid retrying the
    #: same neighbourhood forever.
    recently_freed: frozenset[str] = frozenset()
    #: (student, offering) -> conflict priority, from whatever
    #: `ConflictPriorityProvider` the run is using. Empty means the policy
    #: should fall back to the heuristic. A destroy policy that reads
    #: `criticality()` directly would rank students by the heuristic while the
    #: objective ranked them by an active model, so the search would keep
    #: freeing sessions the objective did not care about.
    priorities: Mapping[tuple[str, str], int] = field(default_factory=dict)

    def scheduled_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self.placement))


@dataclass(frozen=True, slots=True)
class Neighborhood:
    """The sessions a repair may move, and why.

    ``rationale`` is not decoration. SESSION 6 has to explain recommendations
    and SESSION 4 has to justify that LNS helped; a neighbourhood that cannot
    say why it was chosen makes both impossible, and makes a learned policy
    unauditable.
    """

    session_ids: frozenset[str]
    policy_code: str
    rationale: str = ""
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.policy_code:
            raise ValueError("a neighbourhood must name the policy that chose it")

    @property
    def size(self) -> int:
        return len(self.session_ids)

    @property
    def is_empty(self) -> bool:
        return not self.session_ids


@runtime_checkable
class NeighborhoodPolicy(Protocol):
    """Chooses which sessions to free for the next repair.

    Implementations must be **pure with respect to `SearchState`**: the same
    state and seed must yield the same neighbourhood. A policy that consulted
    a clock, a global RNG, or the solver would make every benchmark in
    SESSION 4 unreproducible, and would make a regression impossible to bisect.
    """

    #: Stable identifier, recorded on every iteration metric.
    code: str

    def select(self, state: SearchState) -> Neighborhood: ...
