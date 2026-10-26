"""Routing a change request, and acting on one.

Two things this router does that are worth stating.

**It shows the chain before submission.** `POST /governance/routing` takes an
impact analysis and answers "who will have to approve this". A chain revealed
only after submission makes every escalation feel like a surprise, and a
coordinator who knew their move would reach the dean would often have picked
a different slot.

**It refuses when nothing is configured.** There is no default chain. That is
the playbook's "do not hardcode one universal chain" — see
`domain/governance/routing.py` for why a fallback is the same mistake wearing
a different hat.

The authorisation story is two-layered on purpose. `require(...)` checks that
the caller may act on change requests at all; `apply_action` then checks that
*this* person may take *this* action on *this* step. The first is about the
platform, the second is about the institution's rules, and collapsing them
would put policy in the RBAC table where nobody can configure it.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from academic_ai.api.deps import get_session, require
from academic_ai.api.schemas import (
    ApprovalActionRequest,
    ChainStepModel,
    ImpactResponse,
    RoutingResponse,
    ScopeAssessmentModel,
)
from academic_ai.domain.enums import ScheduleScope
from academic_ai.domain.governance import (
    Chain,
    GovernanceError,
    assess_scope,
    resolve_chain,
)
from academic_ai.domain.scheduling.impact import (
    AffectedCounts,
    ConflictChange,
    ImpactReport,
)
from academic_ai.security.rbac import Permission, Principal

router = APIRouter(prefix="/governance", tags=["governance"])
log = logging.getLogger("academic_ai.api.governance")


def _configured_chains(session: Session) -> dict[ScheduleScope, Chain]:
    """The institution's configured workflows, read from the database.

    SESSION 18 replaced the seeded stub. The behaviour that matters is
    unchanged: a scope with no configured workflow yields nothing here, and
    `resolve_chain` then refuses. That refusal is the requirement, not a gap
    in it -- a fallback default is a universal chain with extra steps, and it
    fails silently because a change routed to the wrong authorities looks
    exactly like one routed to the right ones.
    """
    from academic_ai.infrastructure.repositories import load_chains

    return dict(load_chains(session))


def _report_from(payload: ImpactResponse) -> ImpactReport:
    return ImpactReport(
        moves=(),
        affected=AffectedCounts(
            students=payload.affected.students,
            teachers=payload.affected.teachers,
            programs=payload.affected.programs,
            faculties=payload.affected.faculties,
            rooms=payload.affected.rooms,
            sessions=payload.affected.sessions,
        ),
        new_conflicts=tuple(
            ConflictChange(c.student_id, c.session_a, c.session_b, c.classification)
            for c in payload.new_conflicts
        ),
        resolved_conflicts=(),
        score_delta=tuple(payload.score_delta),
        tier_codes=tuple(payload.tier_codes),
        stability_delta=payload.stability_delta,
    )


@router.post("/routing", response_model=RoutingResponse)
def routing(
    impact: ImpactResponse,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.EDIT_DRAFT_SCHEDULE)),
) -> RoutingResponse:
    """Who would have to approve this change, and why.

    The scope is **derived** from the impact, never declared by the requester:
    a coordinator submitting a move that quietly reaches another faculty
    cannot see that faculty, and asking them to classify their own request
    would make routing depend on the thing they are least placed to know.
    """
    assessment = assess_scope(_report_from(impact))
    try:
        chain = resolve_chain(assessment, _configured_chains(session))
    except GovernanceError as exc:
        # 409, not 422: the request is well-formed and the *institution* has
        # not answered a question only it can answer. A 422 would tell the
        # coordinator they made a mistake.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    log.info(
        "routing resolved",
        extra={
            "user_id": principal.user_id,
            "scope": assessment.scope.value,
            "chain": chain.code,
        },
    )
    return RoutingResponse(
        assessment=ScopeAssessmentModel(
            scope=assessment.scope.value,
            programs=assessment.programs,
            faculties=assessment.faculties,
            rationale=assessment.rationale,
        ),
        chain_code=chain.code,
        steps=[
            ChainStepModel(
                step_number=s.step_number,
                required_role=s.required_role.value,
                requires_all_affected=s.requires_all_affected,
                description=s.description,
            )
            for s in chain.in_order()
        ],
    )
