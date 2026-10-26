"""The approval state machine: five actions, and who may take each.

The chain itself is **data**. `Chain` is whatever the institution configured
for this change's scope, read from `approval_workflow`; nothing here knows
that a coordinator reports to a director, because in some faculties they do
not. What this module knows is how a chain is walked and what each action does
to it.

Four rules the types enforce
============================

**Every request carries an impact analysis.** `RequestState` requires one, and
`apply_action` refuses to advance a request whose impact is empty. The playbook
says every request requires ImpactAnalysis; the schema currently defaults the
column to `{}`, so without this check a request could be approved on no
evidence at all and look identical to one approved on plenty.

**`requires_all_affected` means all of them.** A cross-programme change needs
every affected coordinator, not the first one who happens to look. A step that
collected one approval and moved on would let a change that damages three
programmes be waved through by the one it helps.

**Nobody approves their own request.** The requester is excluded from every
step, whatever role they hold. A director who proposes a change and then
approves it has produced a record of an approval that never happened.

**An AI may not approve.** `REQUEST_AI_ALTERNATIVE` asks a model for options —
that is guidance, and CLAUDE.md permits it. Recording an APPROVE from a
non-human principal is the thing CLAUDE.md forbids, and `security/rbac.py`
already refuses it; this module refuses it again, because the two layers guard
different doors and an approval that reached here without passing RBAC is
exactly the case worth catching.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from typing import Mapping, Sequence

from academic_ai.domain.enums import (
    ApprovalAction,
    ChangeRequestStatus,
    UserRole,
)

__all__ = [
    "ACTION_EFFECTS",
    "TERMINAL_STATUSES",
    "ApprovalDecision",
    "Chain",
    "ChainStep",
    "GovernanceError",
    "PendingStep",
    "RequestState",
    "apply_action",
    "next_pending_step",
    "permitted_actors",
]


class GovernanceError(RuntimeError):
    """A governance action that is refused. Always says who and why."""


TERMINAL_STATUSES = frozenset(
    {
        ChangeRequestStatus.APPROVED,
        ChangeRequestStatus.REJECTED,
        ChangeRequestStatus.WITHDRAWN,
        ChangeRequestStatus.APPLIED,
    }
)

#: What each action does to a request that is under review. Written as a table
#: for the same reason the AI-model lifecycle is: the interesting property is
#: not any single transition, it is that only one path reaches APPROVED.
ACTION_EFFECTS: Mapping[ApprovalAction, str] = {
    ApprovalAction.APPROVE: "advances to the next step, or approves the request",
    ApprovalAction.REJECT: "ends the request",
    ApprovalAction.COUNTERPROPOSE: "returns it to the requester with an alternative",
    ApprovalAction.REQUEST_AI_ALTERNATIVE: "asks for options; decides nothing",
    ApprovalAction.REQUEST_REOPTIMIZATION: "asks for a re-solve; decides nothing",
}


@dataclass(frozen=True, slots=True)
class ChainStep:
    """One configured step. Mirrors `approval_step`."""

    step_number: int
    required_role: UserRole
    #: When true, every affected authority of this role must approve --
    #: "affected Coordinators", plural, from the playbook's cross-program
    #: example. When false, any one holder of the role satisfies the step.
    requires_all_affected: bool = False
    description: str = ""


@dataclass(frozen=True, slots=True)
class Chain:
    """The chain configured for a scope. Data, not code."""

    code: str
    steps: tuple[ChainStep, ...]

    def __post_init__(self) -> None:
        numbers = [s.step_number for s in self.steps]
        if len(set(numbers)) != len(numbers):
            raise GovernanceError(f"chain {self.code}: duplicate step numbers")
        if not self.steps:
            raise GovernanceError(
                f"chain {self.code} has no steps. An empty chain would approve "
                "every request it was given without anybody looking at it."
            )

    def in_order(self) -> tuple[ChainStep, ...]:
        return tuple(sorted(self.steps, key=lambda s: s.step_number))


@dataclass(frozen=True, slots=True)
class ApprovalDecision:
    """One recorded action by one person on one step."""

    step_number: int
    actor_user_id: str
    actor_role: UserRole
    action: ApprovalAction
    comment: str = ""
    at: dt.datetime | None = None
    #: Which affected authority this actor was standing for, when the step
    #: requires all of them.
    on_behalf_of: str | None = None


@dataclass(frozen=True, slots=True)
class RequestState:
    """A change request, as the state machine sees it."""

    request_id: str
    requester_user_id: str
    status: ChangeRequestStatus
    chain: Chain
    #: The measured impact. Required, and required to be non-empty: a request
    #: approved on no evidence looks identical to one approved on plenty.
    impact: Mapping[str, object] = field(default_factory=dict)
    decisions: tuple[ApprovalDecision, ...] = ()
    #: For steps with `requires_all_affected`: who must sign. Supplied by the
    #: caller from the impact, because who is affected is a fact about the
    #: change rather than about the chain.
    required_signatories: Mapping[int, frozenset[str]] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def has_impact(self) -> bool:
        return bool(self.impact)


@dataclass(frozen=True, slots=True)
class PendingStep:
    """The step a request is waiting on, and who is still outstanding."""

    step: ChainStep
    outstanding: frozenset[str] = frozenset()

    @property
    def needs_named_signatories(self) -> bool:
        return self.step.requires_all_affected


def _approvals_for(state: RequestState, step_number: int) -> tuple[ApprovalDecision, ...]:
    return tuple(
        d
        for d in state.decisions
        if d.step_number == step_number and d.action is ApprovalAction.APPROVE
    )


def next_pending_step(state: RequestState) -> PendingStep | None:
    """The step this request is waiting on, or ``None`` when the chain is done.

    A step with `requires_all_affected` is satisfied only when every named
    signatory has approved. Treating "one approval" as enough would let a
    change that damages three programmes be waved through by the one it helps.
    """
    for step in state.chain.in_order():
        approvals = _approvals_for(state, step.step_number)
        if step.requires_all_affected:
            required = state.required_signatories.get(step.step_number, frozenset())
            signed = {d.on_behalf_of or d.actor_user_id for d in approvals}
            outstanding = frozenset(required - signed)
            if outstanding or not required:
                # No required set means nobody has said who must sign, which
                # is not the same as "nobody needs to". Blocking is the safe
                # reading: an unconfigured multi-party step must not silently
                # become a single-party one.
                return PendingStep(step, outstanding or frozenset({"<unspecified>"}))
        elif not approvals:
            return PendingStep(step)
    return None


def permitted_actors(state: RequestState) -> tuple[UserRole, ...]:
    """Roles that may act on this request right now.

    For a UI, so a button does not appear to somebody the action would refuse.
    `apply_action` remains the authority.
    """
    if state.is_terminal:
        return ()
    pending = next_pending_step(state)
    return (pending.step.required_role,) if pending else ()


def apply_action(
    state: RequestState,
    decision: ApprovalDecision,
    *,
    actor_is_human: bool = True,
    now: dt.datetime | None = None,
) -> RequestState:
    """Record one action, or refuse and say why.

    Returns a new state. The caller holds both the before and the after, which
    is what the audit log stores -- a transition recorded only as its result
    cannot be checked against what it claimed to do.
    """
    now = now or dt.datetime.now(dt.timezone.utc)

    if state.is_terminal:
        raise GovernanceError(
            f"request {state.request_id} is already {state.status.value} and "
            "cannot be acted on again"
        )

    if not state.has_impact:
        # The playbook: every request requires ImpactAnalysis. The schema
        # defaults the column to '{}', so without this a request could be
        # approved on no evidence and look identical to one approved on plenty.
        raise GovernanceError(
            f"request {state.request_id} carries no impact analysis. Approving "
            "a change nobody has measured is the thing the workflow exists to "
            "prevent."
        )

    if decision.actor_user_id == state.requester_user_id:
        # Whatever role they hold. A director who proposes a change and then
        # approves it has produced a record of an approval that never happened.
        raise GovernanceError(
            f"{decision.actor_user_id} raised request {state.request_id} and "
            "cannot also decide it"
        )

    pending = next_pending_step(state)
    if pending is None:
        raise GovernanceError(
            f"request {state.request_id} has completed its chain; there is "
            "nothing left to act on"
        )

    if decision.step_number != pending.step.step_number:
        raise GovernanceError(
            f"request {state.request_id} is waiting on step "
            f"{pending.step.step_number}, not {decision.step_number}. Steps are "
            "ordered because a later authority reviewing before an earlier one "
            "is reviewing a different request."
        )

    if decision.actor_role is not pending.step.required_role:
        raise GovernanceError(
            f"step {pending.step.step_number} requires "
            f"{pending.step.required_role.value}; {decision.actor_user_id} is "
            f"{decision.actor_role.value}"
        )

    if decision.action is ApprovalAction.APPROVE and not actor_is_human:
        # RBAC refuses this too. Guarded twice on purpose: an approval that
        # reached here without passing RBAC is exactly the case worth catching.
        raise GovernanceError(
            "an approval must be given by a person. Recording one from a "
            "non-human principal is what CLAUDE.md forbids, whatever role the "
            "principal carries."
        )

    if pending.needs_named_signatories and decision.action is ApprovalAction.APPROVE:
        standing_for = decision.on_behalf_of or decision.actor_user_id
        if standing_for not in pending.outstanding:
            raise GovernanceError(
                f"{standing_for} is not an outstanding signatory for step "
                f"{pending.step.step_number}. Outstanding: "
                + ", ".join(sorted(pending.outstanding))
            )

    if decision.action is ApprovalAction.REJECT and not decision.comment.strip():
        raise GovernanceError(
            "a rejection must say why. A refusal with no reason cannot be "
            "answered, so the requester's only option is to submit it again."
        )

    recorded = replace(decision, at=decision.at or now)
    decisions = state.decisions + (recorded,)

    if decision.action is ApprovalAction.REJECT:
        return replace(state, decisions=decisions, status=ChangeRequestStatus.REJECTED)

    if decision.action is ApprovalAction.COUNTERPROPOSE:
        # Back to the requester. Not a rejection: the reviewer is saying "not
        # this, but perhaps this", and collapsing the two would lose the
        # alternative they took the trouble to write.
        return replace(
            state, decisions=decisions, status=ChangeRequestStatus.COUNTERPROPOSED
        )

    if decision.action in {
        ApprovalAction.REQUEST_AI_ALTERNATIVE,
        ApprovalAction.REQUEST_REOPTIMIZATION,
    }:
        # Recorded, decides nothing, and does not advance the chain. Asking a
        # model for options is guidance (CLAUDE.md); it is not an approval and
        # must not consume a step.
        return replace(
            state, decisions=decisions, status=ChangeRequestStatus.UNDER_REVIEW
        )

    advanced = replace(
        state, decisions=decisions, status=ChangeRequestStatus.UNDER_REVIEW
    )
    if next_pending_step(advanced) is None:
        return replace(advanced, status=ChangeRequestStatus.APPROVED)
    return advanced
