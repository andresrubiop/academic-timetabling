"""Governance: who decides a change, and how that is recorded (SESSION 12).

Framework-free. An approval rule has to be checkable by a test, by the API,
and by whatever runs the notification job, and none of those should need a
request object to answer "may this person approve this".
"""

from academic_ai.domain.governance.routing import required_signatories, resolve_chain
from academic_ai.domain.governance.scope import ScopeAssessment, assess_scope
from academic_ai.domain.governance.workflow import (
    ACTION_EFFECTS,
    ApprovalDecision,
    Chain,
    ChainStep,
    GovernanceError,
    PendingStep,
    RequestState,
    apply_action,
    next_pending_step,
    permitted_actors,
)

__all__ = [
    "ACTION_EFFECTS",
    "ApprovalDecision",
    "Chain",
    "ChainStep",
    "GovernanceError",
    "PendingStep",
    "RequestState",
    "ScopeAssessment",
    "apply_action",
    "assess_scope",
    "required_signatories",
    "resolve_chain",
    "next_pending_step",
    "permitted_actors",
]
