"""Assigning authority over a faculty or a programme.

The write side of `user_scope_assignment`. Kept off the generic CRUD factory
on purpose: this entity carries a rule the factory cannot express — nobody may
grant authority they do not themselves hold — and a table where that rule
lives in a config object is a table where the next maintainer removes it by
deleting a line.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from academic_ai.api.deps import get_session, require
from academic_ai.api.schemas import (
    ScopeGrantModel,
    ScopeGrantRequest,
    ScopeGrantsResponse,
)
from academic_ai.application.scopes import (
    ScopeAssignmentError,
    grant_scope,
    list_scopes,
    revoke_scope,
)
from academic_ai.security.rbac import Permission, Principal

router = APIRouter(prefix="/users", tags=["scopes"])
log = logging.getLogger("academic_ai.api.scopes")


def _model(grant) -> ScopeGrantModel:
    return ScopeGrantModel(
        assignment_id=grant.assignment_id,
        user_id=grant.user_id,
        faculty_id=grant.faculty_id,
        program_id=grant.program_id,
        note=grant.note,
    )


@router.get("/{user_id}/scopes", response_model=ScopeGrantsResponse)
def get_scopes(
    user_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.MANAGE_USERS)),
) -> ScopeGrantsResponse:
    """Everything this user is responsible for."""
    try:
        grants = list_scopes(session, principal, user_id)
    except ScopeAssignmentError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return ScopeGrantsResponse(scopes=[_model(g) for g in grants])


@router.post(
    "/{user_id}/scopes",
    response_model=ScopeGrantModel,
    status_code=status.HTTP_201_CREATED,
)
def post_scope(
    user_id: str,
    payload: ScopeGrantRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.MANAGE_USERS)),
) -> ScopeGrantModel:
    """Give this user authority over one faculty or one programme."""
    try:
        grant = grant_scope(
            session,
            principal,
            user_id,
            faculty_id=payload.faculty_id,
            program_id=payload.program_id,
            note=payload.note,
        )
    except ScopeAssignmentError as exc:
        # 400, not 403: the caller holds `MANAGE_USERS` — what is wrong is the
        # request, and a 403 here would send an administrator to look at their
        # own role rather than at what they asked for.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    session.commit()
    return _model(grant)


@router.delete(
    "/{user_id}/scopes/{assignment_id}", status_code=status.HTTP_204_NO_CONTENT
)
def delete_scope(
    user_id: str,
    assignment_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.MANAGE_USERS)),
) -> None:
    """Take an assignment away. Audited, because the row goes with it."""
    try:
        revoke_scope(session, principal, assignment_id)
    except ScopeAssignmentError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        ) from exc
    session.commit()
