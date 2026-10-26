"""How wide a change reaches, derived from its measured impact.

The playbook is explicit: **do not hardcode one universal chain.** The way to
avoid that is not to write three chains instead of one — it is to stop the
code deciding who approves anything. What the code decides is *how far the
change reaches*; which authorities that requires is configuration, held in
`approval_workflow` rows an institution owns.

The distinction matters because the two questions have different answers in
different universities and only one of them is a fact about the change.
"Which programmes does this touch" is measurable. "Who signs off on a
cross-faculty change" is policy, and a policy compiled into Python is a policy
nobody can change without a deployment.

Derived, never declared
=======================

Scope comes from the `ImpactReport`, not from a field the requester fills in.
A coordinator submitting a move that quietly reaches another faculty is the
normal case — they cannot see that faculty — and asking them to classify their
own request would make the routing depend on the thing they are least placed
to know.
"""

from __future__ import annotations

from dataclasses import dataclass

from academic_ai.domain.enums import ScheduleScope
from academic_ai.domain.scheduling.impact import ImpactReport

__all__ = ["ScopeAssessment", "assess_scope"]


@dataclass(frozen=True, slots=True)
class ScopeAssessment:
    """The reach of a change, and why it was judged that way.

    ``rationale`` exists so a coordinator who expected a two-step approval and
    got a four-step one can see what widened it, rather than concluding the
    system is arbitrary.
    """

    scope: ScheduleScope
    programs: int
    faculties: int
    rationale: str

    @property
    def is_cross_program(self) -> bool:
        return self.programs > 1

    @property
    def is_cross_faculty(self) -> bool:
        return self.faculties > 1


def assess_scope(impact: ImpactReport) -> ScopeAssessment:
    """How far this change reaches.

    Widening is deliberately generous: an unresolvable student conflict
    escalates past the programme even when only one programme's sessions
    moved. A student who can no longer complete their year is not a
    programme-level administrative matter, and routing it as one puts the
    decision in front of somebody without the authority to fix it.
    """
    programs = impact.affected.programs
    faculties = impact.affected.faculties

    if faculties > 1:
        return ScopeAssessment(
            ScheduleScope.INSTITUTION,
            programs,
            faculties,
            f"touches {faculties} faculties, so no single faculty can decide it",
        )

    if any(conflict.is_unresolvable for conflict in impact.new_conflicts):
        # Escalate on harm, not only on breadth.
        return ScopeAssessment(
            ScheduleScope.FACULTY,
            programs,
            faculties,
            "creates a student conflict no re-sectioning can resolve, which is "
            "not a programme-level administrative matter",
        )

    if programs > 1:
        return ScopeAssessment(
            ScheduleScope.FACULTY,
            programs,
            faculties,
            f"touches {programs} programmes, so it needs an authority above "
            "any one of them",
        )

    return ScopeAssessment(
        ScheduleScope.PROGRAM,
        programs,
        faculties,
        "stays inside one programme",
    )
