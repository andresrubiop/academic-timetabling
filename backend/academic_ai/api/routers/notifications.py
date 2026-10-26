"""Your notifications.

Every route is scoped to the authenticated principal by construction: there is
no user parameter, and the queries filter on `recipient_user_id`. An id
belonging to somebody else matches nothing rather than returning 404, because
a 404 would confirm the id exists.

`VIEW_OWN_SCHEDULE` gates the router -- every role holds it. Being signed in
is what entitles you to your own notifications; nothing here reveals anybody
else's.
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from academic_ai.api.deps import get_session, require
from academic_ai.api.schemas import (
    ChannelStatusModel,
    MarkReadRequest,
    MarkReadResponse,
    NotificationListResponse,
    NotificationModel,
)
from academic_ai.application.notifications import (
    _CHANNELS,
    mark_read,
    unread_count,
)
from academic_ai.domain.notifications import ACTIONABLE_KINDS
from academic_ai.security.rbac import Permission, Principal

router = APIRouter(prefix="/notifications", tags=["notifications"])
log = logging.getLogger("academic_ai.api.notifications")

_MAX_PAGE = 100


@router.get("", response_model=NotificationListResponse)
def list_mine(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=25, ge=1, le=_MAX_PAGE),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.VIEW_OWN_SCHEDULE)),
) -> NotificationListResponse:
    """Your notifications, actionable ones first.

    Ordering is deliberate: a request waiting on you outranks a run that
    finished, whatever their timestamps. Sorting purely by recency buries the
    thing somebody is blocked on under an hour of routine completions.
    """
    from academic_ai.infrastructure.db.models import Notification

    user_id = uuid.UUID(principal.user_id)
    mine = Notification.recipient_user_id == user_id

    stmt = select(Notification).where(mine)
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))

    rows = session.scalars(
        stmt.order_by(
            Notification.is_actionable.desc(),
            Notification.read_at.is_(None).desc(),
            Notification.created_at.desc(),
        ).limit(limit)
    ).all()

    total = int(
        session.scalar(select(func.count()).select_from(Notification).where(mine)) or 0
    )
    unread = unread_count(session, principal.user_id)
    unread_actionable = int(
        session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(mine)
            .where(Notification.read_at.is_(None))
            .where(Notification.is_actionable.is_(True))
        )
        or 0
    )

    return NotificationListResponse(
        items=[
            NotificationModel(
                id=str(n.id),
                kind=n.kind.value,
                channel=n.channel.value,
                title=n.title,
                body=n.body,
                payload=n.payload,
                subject_type=n.subject_type,
                subject_id=str(n.subject_id) if n.subject_id else None,
                is_actionable=n.is_actionable,
                is_read=n.read_at is not None,
                created_at=n.created_at,
            )
            for n in rows
        ],
        total=total,
        unread=unread,
        unread_actionable=unread_actionable,
    )


@router.post("/read", response_model=MarkReadResponse)
def mark_as_read(
    payload: MarkReadRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.VIEW_OWN_SCHEDULE)),
) -> MarkReadResponse:
    """Mark some, or all, of your notifications read."""
    marked = mark_read(session, principal.user_id, payload.notification_ids)
    return MarkReadResponse(
        marked=marked, unread=unread_count(session, principal.user_id)
    )


@router.get("/channels", response_model=list[ChannelStatusModel])
def channels(
    principal: Principal = Depends(require(Permission.VIEW_OWN_SCHEDULE)),
) -> list[ChannelStatusModel]:
    """Which delivery channels this deployment actually has.

    Published so nobody has to infer it from silence. EMAIL and PUSH are
    declared and unimplemented; saying so here is the difference between a
    known limitation and an operator believing emails go out.
    """
    del principal
    return [
        ChannelStatusModel(
            channel=channel.value,
            is_available=port.is_available,
            detail=(
                "in-app notifications are written and readable here"
                if port.is_available
                else "declared but not configured on this deployment; nothing "
                "is sent through it and nothing is silently dropped -- the "
                "notification still appears in the in-app list"
            ),
        )
        for channel, port in _CHANNELS.items()
    ]
