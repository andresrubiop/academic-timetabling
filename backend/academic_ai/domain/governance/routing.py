"""Choosing the chain for a change, without inventing one.

`resolve_chain` looks up the workflow an institution configured for the
change's scope. It has exactly one interesting behaviour: **when no workflow
is configured, it refuses.**

That refusal is the whole point of the playbook's "do not hardcode one
universal chain". A fallback default is a universal chain with extra steps: it
is the chain that runs whenever configuration is missing, which on the day
somebody adds a faculty is every cross-faculty change in the institution. And
it fails silently, because a change routed through the wrong authorities looks
exactly like one routed through the right ones.

Refusing produces an error a registrar can act on — "nobody has configured who
approves a cross-faculty change" — at the moment the first such change is
submitted, which is the moment the institution actually has to answer it.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from academic_ai.domain.enums import ScheduleScope
from academic_ai.domain.governance.scope import ScopeAssessment
from academic_ai.domain.governance.workflow import Chain, GovernanceError

__all__ = ["resolve_chain"]


def resolve_chain(
    assessment: ScopeAssessment,
    configured: Mapping[ScheduleScope, Chain],
) -> Chain:
    """The chain for this scope, or a refusal naming what is missing.

    Exact match only. A PROGRAM chain does **not** stand in for a FACULTY one:
    they exist separately precisely because a cross-programme change needs an
    authority above any single programme, and substituting the narrower chain
    would route the change to somebody who cannot decide it while reporting
    success.
    """
    chain = configured.get(assessment.scope)
    if chain is None:
        available = ", ".join(sorted(s.value for s in configured)) or "none"
        raise GovernanceError(
            f"no approval workflow is configured for {assessment.scope.value} "
            f"scope, and this change needs one because it {assessment.rationale}. "
            f"Configured scopes: {available}. Refusing rather than falling back "
            "to a narrower chain: routing a change to authorities who cannot "
            "decide it looks exactly like routing it correctly."
        )
    return chain


def required_signatories(
    chain: Chain,
    affected_by_role: Mapping[str, Sequence[str]],
) -> dict[int, frozenset[str]]:
    """Who must sign each `requires_all_affected` step.

    ``affected_by_role`` maps a role name to the user ids of the people
    holding it who are affected -- the playbook's "affected Coordinators".
    Computed from the impact by the caller, because who is affected is a fact
    about the change rather than about the chain.
    """
    out: dict[int, frozenset[str]] = {}
    for step in chain.in_order():
        if not step.requires_all_affected:
            continue
        out[step.step_number] = frozenset(
            affected_by_role.get(step.required_role.value, ())
        )
    return out
