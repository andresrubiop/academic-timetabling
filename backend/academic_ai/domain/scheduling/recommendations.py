"""`RecommendationProvider` — what to do about a schedule that is wrong.

The second of the three AI contracts named in ADR-0001, so it lives in the
framework-free domain: SESSION 7's model must be able to implement it without
OR-Tools or SQLAlchemy in scope, and be compared against the heuristic
provider through the same interface.

Two rules the playbook states outright, both enforced by the types here.

**Never return only "No solution."** A bare INFEASIBLE is the least useful
true statement a planner can be handed. Every recommendation therefore names
a concrete action on a concrete subject.

**Every recommendation carries structured impact evidence.** Not prose — a
count of affected students, teachers, programmes and rooms, and what it does
to the objective. `ImpactEvidence` is a required field of `Recommendation`,
so a provider cannot return an opinion. A recommendation without evidence is
somebody's guess with an official-looking border around it.

A recommendation is never self-applying. Acting on one produces a
`ChangeRequest` that goes through the approval workflow like any other change,
because an AI may not approve an academic decision (CLAUDE.md — Human
authority).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, runtime_checkable

from academic_ai.domain.enums import RecommendationKind
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.domain.scheduling.problem import ProblemInstance

__all__ = [
    "ImpactEvidence",
    "Recommendation",
    "RecommendationProvider",
    "RecommendationRequest",
]


@dataclass(frozen=True, slots=True)
class ImpactEvidence:
    """Who this would affect, and by how much.

    Counts rather than prose, because SESSION 10 has to render this beside a
    drag-and-drop preview and SESSION 12 has to attach it to an approval. A
    coordinator approving a change is entitled to know how many students it
    moves before they approve it, not afterwards.

    ``score_delta`` is per tier and best-first, matching `ScoreVector`:
    negative is an improvement. A single number would reintroduce the
    cross-tier exchange rate ADR-0010 rejects.
    """

    affected_students: int = 0
    affected_teachers: int = 0
    affected_programs: int = 0
    affected_rooms: int = 0
    moved_sessions: int = 0
    resolved_conflicts: int = 0
    new_conflicts: int = 0
    #: Per-tier change, best-first. Negative means better.
    score_delta: tuple[int, ...] = ()
    tier_codes: tuple[str, ...] = ()

    @property
    def is_improvement(self) -> bool:
        """Lexicographic: the first tier that moves decides."""
        for delta in self.score_delta:
            if delta != 0:
                return delta < 0
        return False

    @property
    def is_neutral(self) -> bool:
        return all(d == 0 for d in self.score_delta)

    def named_delta(self) -> Mapping[str, int]:
        return dict(zip(self.tier_codes, self.score_delta))


@dataclass(frozen=True, slots=True)
class Recommendation:
    """One concrete thing somebody could do.

    ``subject_id`` names what to act on and ``kind`` what to do to it, so a UI
    can turn this into a button rather than a paragraph.
    """

    kind: RecommendationKind
    subject_kind: str
    subject_id: str
    rationale: str
    impact: ImpactEvidence
    #: Where the recommendation came from — a heuristic code, or an AI model
    #: version id. Recorded so a suggestion can be traced to its source and,
    #: if it came from a model, to the model that is answerable for it.
    source: str = "HEURISTIC"
    detail: Mapping[str, object] = field(default_factory=dict)
    #: 0..1 where the provider can say. Heuristics leave it None rather than
    #: inventing a number that would look like a probability.
    confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.rationale.strip():
            raise ValueError(
                f"{self.kind}: a recommendation must say why. An action with no "
                "reason cannot be reviewed, and SESSION 12 requires every "
                "change request to be explainable to the person approving it."
            )
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"{self.kind}: confidence must be in [0, 1]")


@dataclass(frozen=True, slots=True)
class RecommendationRequest:
    """What a provider is asked about.

    ``placement`` is empty when nothing could be scheduled at all — the
    interesting case, and the one where "No solution." would otherwise be the
    entire answer.
    """

    instance: ProblemInstance
    placement: Mapping[str, PlacedSession] = field(default_factory=dict)
    #: Restrict the answer to these sessions, when the caller is asking about
    #: one problem rather than the whole timetable.
    focus_session_ids: frozenset[str] = frozenset()
    max_recommendations: int = 20


@runtime_checkable
class RecommendationProvider(Protocol):
    """Suggests actions. Never takes them.

    A provider returns recommendations ordered best-first by its own judgement.
    It may not modify the instance, may not decide feasibility, and may not
    approve anything — those are CP-SAT's job and a human's job respectively.
    """

    #: Stable identifier, recorded on every recommendation it produces.
    code: str

    def recommend(self, request: RecommendationRequest) -> Sequence[Recommendation]: ...
