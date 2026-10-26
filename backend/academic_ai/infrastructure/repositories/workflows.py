"""Reading an institution's approval workflows into governance `Chain`s.

Replaces the seeded stub SESSION 12 left behind. The behaviour that matters is
unchanged and is the whole point of the design: **a scope with no configured
workflow produces nothing**, and `resolve_chain` then refuses. A fallback
default would be a universal chain with extra steps -- it runs whenever
configuration is missing, and it fails silently, because a change routed to
the wrong authorities looks exactly like one routed to the right ones.

So this loader can return an empty mapping, and that is a correct answer.
"""

from __future__ import annotations

import logging
import uuid
from typing import Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.domain.enums import ScheduleScope
from academic_ai.domain.governance import Chain, ChainStep, GovernanceError

__all__ = ["load_chains"]

log = logging.getLogger("academic_ai.repositories.workflows")


def load_chains(
    session: Session, institution_id: uuid.UUID | str | None = None
) -> Mapping[ScheduleScope, Chain]:
    """The configured chain for each scope, if there is one.

    Where an institution has configured two active workflows for one scope,
    the one with the lower code wins deterministically rather than whichever
    the database happened to return first -- a routing decision that changed
    between two identical requests would be indistinguishable from a bug.
    """
    from academic_ai.infrastructure.db.models import ApprovalWorkflow

    stmt = select(ApprovalWorkflow).where(ApprovalWorkflow.is_active.is_(True))
    if institution_id is not None:
        stmt = stmt.where(
            ApprovalWorkflow.institution_id
            == (
                institution_id
                if isinstance(institution_id, uuid.UUID)
                else uuid.UUID(str(institution_id))
            )
        )

    chains: dict[ScheduleScope, Chain] = {}
    for workflow in session.scalars(stmt.order_by(ApprovalWorkflow.code)):
        if workflow.applies_to_scope in chains:
            log.warning(
                "a second active workflow for this scope was ignored",
                extra={
                    "scope": workflow.applies_to_scope.value,
                    "ignored": workflow.code,
                },
            )
            continue
        try:
            chains[workflow.applies_to_scope] = Chain(
                code=workflow.code,
                steps=tuple(
                    ChainStep(
                        step_number=step.step_number,
                        required_role=step.required_role,
                        requires_all_affected=step.requires_all_affected,
                        description=step.description or "",
                    )
                    for step in sorted(
                        workflow.steps, key=lambda s: s.step_number
                    )
                ),
            )
        except GovernanceError:
            # A workflow with no steps would approve every request it was
            # given without anybody looking. Skipping it means the scope has
            # no chain and `resolve_chain` refuses, which is the safe answer.
            log.exception(
                "an approval workflow could not be read and was skipped",
                extra={"workflow": workflow.code},
            )

    log.info(
        "approval chains loaded",
        extra={"scopes": sorted(s.value for s in chains)},
    )
    return chains
