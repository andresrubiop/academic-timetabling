"""Structural changes to a class session.

Deliberately **not** under `/schedule`. An invariant test asserts that no
route beneath that prefix mutates anything (ADR-0012: the accepted schedule is
never writable from an interaction), and it caught this endpoint the first
time it was put there — correctly. Splitting a class changes what the
institution teaches, not when it teaches it, so it belongs with the sessions
rather than with the timetable.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from academic_ai.api.deps import get_session, require
from academic_ai.api.schemas import SplitResultModel, SplitSessionRequest
from academic_ai.application.split import SplitError, split_session
from academic_ai.security.rbac import Permission, Principal

router = APIRouter(prefix="/sessions", tags=["sessions"])
log = logging.getLogger("academic_ai.api.sessions")


@router.post("/{class_session_id}/split", response_model=SplitResultModel)
def split(
    class_session_id: str,
    payload: SplitSessionRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.EDIT_DRAFT_SCHEDULE)),
) -> SplitResultModel:
    """Divide a class that runs several hours into two separate classes.

    Not something a drag does. `ClassSession` is the schedulable unit — one
    weekly meeting block — so moving one hour of a three-hour class is not a
    move but a statement that it is now two meetings, and that is asked for by
    name rather than done as a side effect of tidying a Wednesday.
    """
    try:
        result = split_session(
            session,
            principal,
            class_session_id,
            first_part_slots=payload.first_part_slots,
        )
    except SplitError as exc:
        # 409: nothing is wrong with the request, the thing it names is in a
        # state that forbids it — published, or already one hour long.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        ) from exc
    session.commit()
    return SplitResultModel(
        original_session_id=result.original_session_id,
        new_session_id=result.new_session_id,
        original_slots=result.original_slots,
        new_slots=result.new_slots,
    )
