"""The viewer's dashboard.

One endpoint, and it takes no role parameter. That is the point: a dashboard
is assembled from the authenticated principal's own permissions, so there is
no request a client can make that asks for somebody else's screen.

`VIEW_OWN_SCHEDULE` is the gate, which every role holds. It is not the
authority for anything shown -- each widget carries its own permission and is
dropped if the viewer lacks it -- it is only the statement that being signed
in entitles you to *a* dashboard.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from academic_ai.api.deps import get_session, require
from academic_ai.api.schemas import DashboardResponse, MetricModel
from academic_ai.application.dashboards import build_dashboard
from academic_ai.security.rbac import Permission, Principal

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
log = logging.getLogger("academic_ai.api.dashboard")


@router.get("", response_model=DashboardResponse)
def my_dashboard(
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.VIEW_OWN_SCHEDULE)),
) -> DashboardResponse:
    """Everything this viewer is entitled to see, and nothing else."""
    metrics = build_dashboard(session, principal)
    log.info(
        "dashboard served",
        extra={
            "user_id": principal.user_id,
            "role": principal.role.value,
            "panels": len(metrics),
        },
    )
    return DashboardResponse(
        role=principal.role.value,
        metrics=[
            MetricModel(
                widget_code=m.widget_code,
                title=m.title,
                kind=m.kind.value,
                value=m.value,
                unit=m.unit,
                severity=m.severity.value,
                rows=[dict(r) for r in m.rows],
                detail=m.detail,
                is_unavailable=m.is_unavailable,
            )
            for m in metrics
        ],
    )
