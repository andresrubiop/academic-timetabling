"""Submitting a change request, and deciding one.

The write path SESSION 10's `CREATE CHANGE REQUEST` button was reporting as
missing and SESSION 12's state machine was waiting for.

Nothing here applies anything. A submitted request changes no timetable and an
approved one changes no timetable either -- applying is a separate act against
a schedule version, and keeping it separate is what makes the chain mean
something rather than being a delay before an inevitability.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.api.deps import get_session, require
from academic_ai.api.routers.schedule import _to_impact_response
from academic_ai.api.schemas import (
    AppliedChangeModel,
    ChangeRequestListResponse,
    ChangeRequestSummaryModel,
    ChainStepModel,
    ChangeRequestStateResponse2,
    DecideRequest,
    DecisionModel,
    SubmitChangeRequest,
    SubmitChangeRequestResponse,
)
from academic_ai.application.change_requests import (
    ChangeRequestError,
    decide,
    load_state,
    submit_change_request,
)
from academic_ai.domain.enums import ApprovalAction
from academic_ai.domain.governance import next_pending_step
from academic_ai.domain.scheduling.impact import ProposedMove
from academic_ai.security.rbac import Permission, Principal

router = APIRouter(prefix="/change-requests", tags=["governance"])
log = logging.getLogger("academic_ai.api.change_requests")


@router.post(
    "", response_model=SubmitChangeRequestResponse, status_code=status.HTTP_201_CREATED
)
def submit(
    payload: SubmitChangeRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.EDIT_DRAFT_SCHEDULE)),
) -> SubmitChangeRequestResponse:
    """File a change request from a set of proposed moves.

    201, not 202: the *request* exists once this returns. The change it asks
    for does not, and will not until somebody approves it -- which is what the
    status says.
    """
    moves = [
        ProposedMove(m.session_id, m.to_start_slot_id, m.to_room_id)
        for m in payload.moves
    ]
    try:
        result = submit_change_request(
            session,
            principal,
            academic_period_id=payload.academic_period_id,
            moves=moves,
            title=payload.title,
            description=payload.description,
        )
    except ChangeRequestError as exc:
        # 409: the request is well-formed, and something about the
        # institution's state -- no published timetable, no configured chain
        # -- prevents it. A 422 would tell the coordinator they made a mistake.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    return SubmitChangeRequestResponse(
        change_request_id=result.change_request_id,
        code=result.code,
        status=result.status.value,
        chain_code=result.chain_code,
        scope=result.scope,
        rationale=result.rationale,
        impact=_to_impact_response(result.impact, result.impact.moves),
        notified=result.notified,
    )


@router.get("", response_model=ChangeRequestListResponse)
def list_requests(
    academic_period_id: str | None = None,
    status_filter: str | None = None,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.VIEW_PROGRAM_SCHEDULE)),
) -> ChangeRequestListResponse:
    """The requests this person has any business seeing.

    **Narrowed by jurisdiction, plus your own.** A coordinator sees the
    requests that reach a programme they hold — which is what
    `affected_program_ids` was recorded for in SESSION 23 — and always sees
    what they submitted themselves. Without that second half your own request
    disappears from your list the moment it touches somebody else's
    programme, which is exactly when you most want to follow it.

    This is the first place row-level scope reaches past the CRUD surface. It
    works here because a request records *which* programmes it affects; the
    timetable does not, which is why `GET /schedule` still returns a whole
    period.
    """
    from academic_ai.domain.enums import ChangeRequestStatus
    from academic_ai.infrastructure.db.models import ChangeRequest

    stmt = select(ChangeRequest).order_by(ChangeRequest.created_at.desc())
    if academic_period_id:
        stmt = stmt.where(
            ChangeRequest.academic_period_id == uuid.UUID(academic_period_id)
        )
    if status_filter:
        try:
            stmt = stmt.where(
                ChangeRequest.status == ChangeRequestStatus[status_filter]
            )
        except KeyError:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"{status_filter!r} is not a change-request status",
            ) from None

    jurisdiction = principal.jurisdiction
    rows = []
    for request in session.scalars(stmt):
        impact = dict(request.impact or {})
        affected = [str(p) for p in impact.get("affected_program_ids", [])]
        is_mine = str(request.requested_by_user_id) == principal.user_id
        if not is_mine and not jurisdiction.is_institution_wide:
            # A request that records no affected programmes is visible: it is
            # older than SESSION 23 or genuinely touched none, and hiding it
            # would make the list quietly incomplete rather than narrowed.
            if affected and not jurisdiction.covers_any_program(affected):
                continue
        counts = impact.get("affected", {}) or {}
        rows.append(
            ChangeRequestSummaryModel(
                request_id=str(request.id),
                code=request.code,
                title=request.title,
                status=request.status.value,
                academic_period_id=str(request.academic_period_id),
                requested_by_user_id=str(request.requested_by_user_id),
                created_at=request.created_at.isoformat(),
                summary=str(impact.get("summary", "")),
                scope=str(impact.get("scope", "")),
                affected_students=int(counts.get("students", 0) or 0),
                new_conflicts=int(impact.get("new_conflicts", 0) or 0),
                is_mine=is_mine,
            )
        )
    return ChangeRequestListResponse(requests=rows)


@router.get("/{request_id}", response_model=ChangeRequestStateResponse2)
def get_one(
    request_id: uuid.UUID,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.VIEW_PROGRAM_SCHEDULE)),
) -> ChangeRequestStateResponse2:
    from academic_ai.infrastructure.db.models import ChangeRequest

    request = session.get(ChangeRequest, request_id)
    if request is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    state = load_state(session, request_id)
    pending = next_pending_step(state)
    return ChangeRequestStateResponse2(
        request_id=str(request.id),
        code=request.code,
        title=request.title,
        status=state.status.value,
        is_terminal=state.is_terminal,
        chain_code=state.chain.code,
        pending_step=(
            ChainStepModel(
                step_number=pending.step.step_number,
                required_role=pending.step.required_role.value,
                requires_all_affected=pending.step.requires_all_affected,
                description=pending.step.description,
                outstanding=sorted(pending.outstanding),
            )
            if pending
            else None
        ),
        decisions=[
            DecisionModel(
                step_number=d.step_number,
                actor_user_id=d.actor_user_id,
                actor_role=d.actor_role.value,
                action=d.action.value,
                comment=d.comment,
                on_behalf_of=d.on_behalf_of,
            )
            for d in state.decisions
        ],
        impact=dict(state.impact),
    )


@router.post("/{request_id}/decide", response_model=ChangeRequestStateResponse2)
def decide_one(
    request_id: uuid.UUID,
    payload: DecideRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.VIEW_PROGRAM_SCHEDULE)),
) -> ChangeRequestStateResponse2:
    """Approve, reject, counterpropose, or ask for help.

    `VIEW_PROGRAM_SCHEDULE` is only the platform-level gate. Whether *this*
    person may take *this* action on *this* step is the governance state
    machine's, and it refuses far more than RBAC does -- nobody decides their
    own request, steps run in order, and an AI may not record an approval.
    """
    try:
        action = ApprovalAction(payload.action)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="action must be one of: "
            + ", ".join(a.value for a in ApprovalAction),
        ) from None

    try:
        decide(
            session,
            principal,
            request_id,
            action,
            comment=payload.comment,
            on_behalf_of=payload.on_behalf_of,
            counterproposal=payload.counterproposal,
        )
    except ChangeRequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc

    return get_one(request_id, session, principal)


@router.post("/{request_id}/apply", response_model=AppliedChangeModel)
def apply(
    request_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.PUBLISH_SCHEDULE)),
) -> AppliedChangeModel:
    """Publish a new timetable that includes this approved change.

    Gated on `PUBLISH_SCHEDULE` rather than on having approved: an approval is
    permission to apply, not the application. See `application/apply.py`.
    """
    from academic_ai.application.apply import ApplyError, apply_change_request

    try:
        applied = apply_change_request(session, principal, request_id)
    except ApplyError as exc:
        # 409, not 400: nothing is wrong with the request itself. Its *state*
        # or the timetable's has moved — a stale approval, an already-applied
        # one — and the caller's next step is to look at what changed rather
        # than to fix what they sent.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    session.commit()
    return AppliedChangeModel(
        change_request_id=applied.change_request_id,
        code=applied.code,
        schedule_version_id=applied.schedule_version_id,
        version_number=applied.version_number,
        superseded_version_id=applied.superseded_version_id,
        moved_sessions=applied.moved_sessions,
    )
