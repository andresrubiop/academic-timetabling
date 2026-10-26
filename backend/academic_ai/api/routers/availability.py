"""Reading and writing when somebody cannot teach.

The most common thing anybody wants to say to a timetabling system — "she does
not teach Friday afternoons" — and until now there was no way to say it. The
model, the HARD constraint and the snapshot read all existed; nothing wrote.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from academic_ai.api.deps import get_session, require
from academic_ai.api.schemas import AvailabilityModel, SetAvailabilityRequest
from academic_ai.application.availability import (
    AvailabilityError,
    load_availability,
    set_availability,
)
from academic_ai.security.rbac import Permission, Principal

router = APIRouter(prefix="/availability", tags=["availability"])
log = logging.getLogger("academic_ai.api.availability")


def _model(result) -> AvailabilityModel:
    return AvailabilityModel(
        owner_kind=result.owner_kind,
        owner_id=result.owner_id,
        unavailable_slot_ids=list(result.unavailable_slot_ids),
        has_declared=result.has_declared,
        reason=result.reason,
    )


@router.get("/{owner_kind}/{owner_id}", response_model=AvailabilityModel)
def get_availability(
    owner_kind: str,
    owner_id: str,
    academic_period_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.VIEW_PROGRAM_SCHEDULE)),
) -> AvailabilityModel:
    try:
        return _model(
            load_availability(
                session,
                principal,
                owner_kind=owner_kind,
                owner_id=owner_id,
                academic_period_id=academic_period_id,
            )
        )
    except AvailabilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.put("/{owner_kind}/{owner_id}", response_model=AvailabilityModel)
def put_availability(
    owner_kind: str,
    owner_id: str,
    payload: SetAvailabilityRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.MANAGE_CONSTRAINTS)),
) -> AvailabilityModel:
    """Replace this owner's blocked hours for one period.

    A PUT rather than a PATCH because the body is the whole calendar: what is
    absent is available, which is the same rule the database uses.
    """
    try:
        result = set_availability(
            session,
            principal,
            owner_kind=owner_kind,
            owner_id=owner_id,
            academic_period_id=payload.academic_period_id,
            unavailable_slot_ids=payload.unavailable_slot_ids,
            reason=payload.reason,
        )
    except AvailabilityError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    session.commit()
    return _model(result)
