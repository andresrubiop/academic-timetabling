"""Turning a proposal into a change request, and deciding one.

This is the write path SESSION 10's button and SESSION 12's state machine were
both waiting on, and it is where three earlier decisions finally meet.

**The impact is recomputed here, never accepted from the client.** A request
that carried its own impact analysis would let somebody submit a change
claiming it affects nobody, and the routing -- which is derived *from* the
impact -- would then send it to the shortest chain. So the client sends the
moves; the server works out what they do.

**The scenario is written at submission, not before.** `run_scenario` is pure
and writes nothing, which is what keeps a what-if isolated: a coordinator
exploring five options should not leave five rows behind. The row appears at
the moment somebody asks to commit to one, because a change request has to
point at something.

**Nothing here applies anything.** A submitted request changes no timetable; an
approved one changes no timetable either. Applying is a separate act against a
schedule version, and keeping it separate is what makes the approval chain
mean something.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass
from typing import Mapping, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from academic_ai.domain.enums import (
    ApprovalAction,
    ChangeRequestStatus,
    NotificationKind,
    ScenarioStatus,
    ScheduleVersionStatus,
    UserRole,
)
from academic_ai.domain.governance import (
    ApprovalDecision,
    GovernanceError,
    RequestState,
    apply_action,
    assess_scope,
    next_pending_step,
    required_signatories,
    resolve_chain,
)
from academic_ai.domain.notifications import NotificationRequest
from academic_ai.domain.scheduling.impact import ImpactReport, ProposedMove, analyse_impact
from academic_ai.domain.scheduling.neighborhood import PlacedSession
from academic_ai.security.rbac import ActorKind, Permission, Principal, authorise

__all__ = [
    "ChangeRequestError",
    "SubmissionResult",
    "decide",
    "load_state",
    "submit_change_request",
]

log = logging.getLogger("academic_ai.change_requests")


class ChangeRequestError(RuntimeError):
    """The request cannot be created or decided as asked."""


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    change_request_id: str
    code: str
    status: ChangeRequestStatus
    chain_code: str
    scope: str
    rationale: str
    impact: ImpactReport
    notified: int = 0


# ---------------------------------------------------------------------------
# Submitting
# ---------------------------------------------------------------------------


def submit_change_request(
    session: Session,
    principal: Principal,
    *,
    academic_period_id: uuid.UUID | str,
    moves: Sequence[ProposedMove],
    title: str,
    description: str = "",
    now: dt.datetime | None = None,
) -> SubmissionResult:
    """Create a change request from a set of proposed moves.

    Writes a scenario, its perturbations, and the request. Applies nothing.
    """
    from academic_ai.infrastructure.db.models import (
        ChangeRequest,
        Scenario,
        ScenarioPerturbation,
        ScheduleVersion,
    )
    from academic_ai.infrastructure.repositories import (
        SnapshotLoadError,
        load_chains,
        load_constraints,
        load_snapshot,
    )

    authorise(principal, Permission.EDIT_DRAFT_SCHEDULE)
    now = now or dt.datetime.now(dt.timezone.utc)
    period_id = _as_uuid(academic_period_id)

    if not moves:
        raise ChangeRequestError(
            "a change request must propose at least one move; an empty one "
            "would go through an approval chain to change nothing"
        )
    if not title.strip():
        raise ChangeRequestError(
            "a change request must have a title. Somebody two steps up the "
            "chain will see it before they see anything else."
        )

    version = session.scalars(
        select(ScheduleVersion)
        .where(ScheduleVersion.academic_period_id == period_id)
        .where(ScheduleVersion.status == ScheduleVersionStatus.PUBLISHED)
        .order_by(ScheduleVersion.published_at.desc())
        .limit(1)
    ).first()
    if version is None:
        raise ChangeRequestError(
            "there is no published timetable to change; a draft is edited "
            "directly rather than through the approval chain"
        )

    # --- what would this actually do -------------------------------------
    try:
        snapshot, _ = load_snapshot(
            session, period_id, base_schedule_version_id=version.id
        )
    except SnapshotLoadError as exc:
        raise ChangeRequestError(str(exc)) from exc

    from academic_ai.domain.scheduling.objective import OBJECTIVE_PROFILES
    from academic_ai.domain.scheduling.problem import ProblemInstance

    instance = ProblemInstance(
        snapshot=snapshot,
        constraints=load_constraints(session, period_id),
        objective_profile=OBJECTIVE_PROFILES["MINIMAL_DISRUPTION"],
    )
    placement = {
        b.session_id: PlacedSession(b.session_id, b.start_slot_id, b.room_id)
        for b in snapshot.base_assignments
    }
    unknown = sorted({m.session_id for m in moves} - set(placement))
    if unknown:
        raise ChangeRequestError(
            "these sessions are not in the published timetable and cannot be "
            "moved: " + ", ".join(unknown[:5])
        )

    # Recomputed, never taken from the caller: a client-supplied impact would
    # let somebody submit a change claiming it affects nobody, and the routing
    # is derived from the impact.
    impact = analyse_impact(instance, placement, moves)

    # --- who has to agree --------------------------------------------------
    assessment = assess_scope(impact)
    chains = load_chains(session)
    try:
        chain = resolve_chain(assessment, chains)
    except GovernanceError as exc:
        raise ChangeRequestError(str(exc)) from exc

    # --- the scenario this request points at -------------------------------
    scenario = Scenario(
        base_schedule_version_id=version.id,
        code=_next_code(session, Scenario, "SC"),
        name=title[:255],
        description=description[:2048] or None,
        # OPEN, not PROMOTED: the scenario exists and has been evaluated,
        # but nothing has adopted it. PROMOTED would claim the timetable
        # already reflects it.
        status=ScenarioStatus.OPEN,
        created_by_user_id=_as_uuid(principal.user_id),
    )
    session.add(scenario)
    session.flush()

    for index, move in enumerate(moves, start=1):
        session.add(
            ScenarioPerturbation(
                scenario_id=scenario.id,
                sequence_number=index,
                kind="MOVE_SESSION",
                class_session_id=_as_uuid(move.session_id),
                timeslot_id=_as_uuid(move.to_start_slot_id),
                room_id=_as_uuid(move.to_room_id) if move.to_room_id else None,
                payload={
                    "start_slot_id": move.to_start_slot_id,
                    "room_id": move.to_room_id,
                },
                created_by_user_id=_as_uuid(principal.user_id),
            )
        )

    # --- the request -------------------------------------------------------
    request = ChangeRequest(
        academic_period_id=period_id,
        code=_next_code(session, ChangeRequest, "CR"),
        title=title[:255],
        description=description[:4096] or None,
        requested_by_user_id=_as_uuid(principal.user_id),
        approval_workflow_id=_workflow_id_for(session, chain.code),
        base_schedule_version_id=version.id,
        scenario_id=scenario.id,
        status=ChangeRequestStatus.SUBMITTED,
        impact=_impact_payload(impact, assessment),
        created_by_user_id=_as_uuid(principal.user_id),
    )
    session.add(request)
    session.flush()

    notified = _notify_next_step(session, request, chain, impact, principal, now)

    log.info(
        "change request submitted",
        extra={
            "request_code": request.code,
            "scope": assessment.scope.value,
            "chain": chain.code,
            "user_id": principal.user_id,
        },
    )
    return SubmissionResult(
        change_request_id=str(request.id),
        code=request.code,
        status=request.status,
        chain_code=chain.code,
        scope=assessment.scope.value,
        rationale=assessment.rationale,
        impact=impact,
        notified=notified,
    )


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def load_state(session: Session, request_id: uuid.UUID | str) -> RequestState:
    """The request as the governance state machine sees it."""
    from academic_ai.infrastructure.db.models import (
        Approval,
        ApprovalStep,
        ApprovalWorkflow,
        ChangeRequest,
    )
    from academic_ai.domain.governance import Chain, ChainStep

    request = session.get(ChangeRequest, _as_uuid(request_id))
    if request is None:
        raise ChangeRequestError(f"no change request {request_id}")

    workflow = session.get(ApprovalWorkflow, request.approval_workflow_id)
    if workflow is None:  # pragma: no cover - foreign key guarantees it
        raise ChangeRequestError("this request's approval workflow is missing")

    chain = Chain(
        code=workflow.code,
        steps=tuple(
            ChainStep(
                step_number=s.step_number,
                required_role=s.required_role,
                requires_all_affected=s.requires_all_affected,
                description=s.description or "",
            )
            for s in sorted(workflow.steps, key=lambda s: s.step_number)
        ),
    )

    decisions = []
    for approval in session.scalars(
        select(Approval)
        .where(Approval.change_request_id == request.id)
        .order_by(Approval.created_at)
    ):
        step = session.get(ApprovalStep, approval.approval_step_id)
        actor = _role_of(session, approval.actor_user_id)
        decisions.append(
            ApprovalDecision(
                step_number=step.step_number if step else 0,
                actor_user_id=str(approval.actor_user_id),
                actor_role=actor,
                action=approval.action,
                comment=approval.comment or "",
                at=approval.created_at,
                on_behalf_of=(approval.counterproposal or {}).get("on_behalf_of"),
            )
        )

    signatories = required_signatories(
        chain, _affected_by_role(session, request)
    )

    return RequestState(
        request_id=str(request.id),
        requester_user_id=str(request.requested_by_user_id),
        status=request.status,
        chain=chain,
        impact=dict(request.impact or {}),
        decisions=tuple(decisions),
        required_signatories=signatories,
    )


def decide(
    session: Session,
    principal: Principal,
    request_id: uuid.UUID | str,
    action: ApprovalAction,
    *,
    comment: str = "",
    on_behalf_of: str | None = None,
    counterproposal: Mapping[str, object] | None = None,
    now: dt.datetime | None = None,
) -> RequestState:
    """Record one decision, or refuse and say why.

    Authorisation is two-layered on purpose. RBAC says whether this principal
    may act on change requests at all; the governance state machine says
    whether *this* person may take *this* action on *this* step. The first is
    about the platform, the second about the institution's own rules, and
    collapsing them would put policy in the RBAC table where nobody can
    configure it.
    """
    from academic_ai.infrastructure.db.models import (
        Approval,
        ApprovalStep,
        ChangeRequest,
    )

    authorise(principal, Permission.VIEW_PROGRAM_SCHEDULE)
    now = now or dt.datetime.now(dt.timezone.utc)

    request = session.get(ChangeRequest, _as_uuid(request_id))
    if request is None:
        raise ChangeRequestError(f"no change request {request_id}")

    state = load_state(session, request.id)
    pending = next_pending_step(state)
    if pending is None:
        raise ChangeRequestError(
            f"request {request.code} has completed its chain; there is nothing "
            "left to act on"
        )

    decision = ApprovalDecision(
        step_number=pending.step.step_number,
        actor_user_id=principal.user_id,
        actor_role=principal.role,
        action=action,
        comment=comment,
        on_behalf_of=on_behalf_of,
    )

    try:
        updated = apply_action(
            state, decision, actor_is_human=principal.kind is ActorKind.HUMAN, now=now
        )
    except GovernanceError as exc:
        raise ChangeRequestError(str(exc)) from exc

    step_row = session.scalars(
        select(ApprovalStep)
        .where(ApprovalStep.approval_workflow_id == request.approval_workflow_id)
        .where(ApprovalStep.step_number == pending.step.step_number)
    ).first()

    payload = dict(counterproposal or {})
    if on_behalf_of:
        payload["on_behalf_of"] = on_behalf_of

    session.add(
        Approval(
            change_request_id=request.id,
            approval_step_id=step_row.id,
            actor_user_id=_as_uuid(principal.user_id),
            action=action,
            comment=comment or None,
            counterproposal=payload or None,
        )
    )
    request.status = updated.status
    session.flush()

    _notify_decision(session, request, updated, action, principal, now)

    log.info(
        "change request decided",
        extra={
            "request_code": request.code,
            "action": action.value,
            "new_status": updated.status.value,
            "user_id": principal.user_id,
        },
    )
    return updated


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def _notify_next_step(
    session: Session, request, chain, impact, principal: Principal, now
) -> int:
    """Tell whoever the chain is now waiting on -- and only them.

    Notifying every approver in the chain would tell the third one about a
    request seven days before it reaches them, which trains people to ignore
    the message that eventually matters (SESSION 17).
    """
    from academic_ai.application.notifications import dispatch

    state = load_state(session, request.id)
    pending = next_pending_step(state)
    if pending is None:
        return 0

    recipients = _users_with_role(session, pending.step.required_role)
    report = dispatch(
        session,
        [
            NotificationRequest(
                kind=NotificationKind.APPROVAL_REQUIRED,
                recipient_user_id=str(user_id),
                title=f"{request.code}: {request.title}",
                body=(
                    f"Affects {impact.affected.students} student(s) and "
                    f"{impact.affected.teachers} teacher(s). {impact.summary()}"
                ),
                subject_type="change_request",
                subject_id=str(request.id),
                actor_user_id=principal.user_id,
            )
            for user_id in recipients
        ],
    )
    return report.delivered


def _notify_decision(
    session: Session, request, state: RequestState, action, principal, now
) -> None:
    from academic_ai.application.notifications import dispatch

    kinds = {
        ApprovalAction.APPROVE: NotificationKind.REQUEST_APPROVED,
        ApprovalAction.REJECT: NotificationKind.REQUEST_REJECTED,
        ApprovalAction.COUNTERPROPOSE: NotificationKind.COUNTERPROPOSAL,
    }
    requests: list[NotificationRequest] = []

    kind = kinds.get(action)
    if kind is not None and state.status is not ChangeRequestStatus.UNDER_REVIEW:
        # The requester hears the outcome. `suppress_actor` drops it if they
        # decided it themselves, which the state machine already forbids.
        requests.append(
            NotificationRequest(
                kind=kind,
                recipient_user_id=state.requester_user_id,
                title=f"{request.code}: {request.title}",
                body=f"{action.value.lower()} — {request.status.value}",
                subject_type="change_request",
                subject_id=str(request.id),
                actor_user_id=principal.user_id,
            )
        )

    # Still moving: tell whoever it now waits on.
    pending = next_pending_step(state)
    if pending is not None and action is ApprovalAction.APPROVE:
        requests.extend(
            NotificationRequest(
                kind=NotificationKind.APPROVAL_REQUIRED,
                recipient_user_id=str(user_id),
                title=f"{request.code}: {request.title}",
                body="This request now needs your decision.",
                subject_type="change_request",
                subject_id=str(request.id),
                actor_user_id=principal.user_id,
            )
            for user_id in _users_with_role(session, pending.step.required_role)
        )

    if requests:
        dispatch(session, requests)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _users_with_role(session: Session, role: UserRole) -> tuple[uuid.UUID, ...]:
    from academic_ai.infrastructure.db.models import User

    return tuple(
        session.scalars(
            select(User.id)
            .where(User.primary_role == role)
            .where(User.is_active.is_(True))
        )
    )


def _role_of(session: Session, user_id: uuid.UUID) -> UserRole:
    from academic_ai.infrastructure.db.models import User

    user = session.get(User, user_id)
    return user.primary_role if user else UserRole.STUDENT


def _affected_by_role(session: Session, request) -> Mapping[str, Sequence[str]]:
    """Who must sign a `requires_all_affected` step.

    Narrowed in SESSION 23 to the coordinators of the programmes the change
    actually reaches. Until then this returned *every* coordinator, because
    the schema carried no programme-to-user mapping — `user_scope_assignment`
    is that mapping.

    **Erring wide is still the fallback, and deliberately so.** Two cases fall
    back to every coordinator:

    * the change records no affected programmes — an older request stored
      before the ids were kept, or one whose impact genuinely touched none;
    * narrowing would leave *nobody*, because no coordinator is assigned to
      the affected programmes.

    The second is the one worth being careful about. A `requires_all_affected`
    step with an empty signatory list is satisfied by nobody signing, so
    narrowing to zero does not make the change harder to approve — it makes
    the step vanish. Asking too many people is recoverable; a change passing
    a step nobody had to sign is not, and it would look identical to working.
    """
    from academic_ai.infrastructure.repositories import load_jurisdictions

    everybody = [str(u) for u in _users_with_role(session, UserRole.COORDINATOR)]

    affected = [str(p) for p in (request.impact or {}).get("affected_program_ids", [])]
    if not affected or not everybody:
        return {UserRole.COORDINATOR.value: everybody}

    jurisdictions = load_jurisdictions(session, everybody, UserRole.COORDINATOR)
    narrowed = [
        user_id
        for user_id in everybody
        if jurisdictions[user_id].covers_any_program(affected)
    ]

    if not narrowed:
        log.warning(
            "no coordinator is assigned to the affected programmes; asking all "
            "of them rather than none",
            extra={
                "change_request": request.code,
                "affected_programs": len(affected),
                "coordinators": len(everybody),
            },
        )
        return {UserRole.COORDINATOR.value: everybody}

    return {UserRole.COORDINATOR.value: narrowed}


def _impact_payload(impact: ImpactReport, assessment) -> dict:
    return {
        "affected": {
            "students": impact.affected.students,
            "teachers": impact.affected.teachers,
            "programs": impact.affected.programs,
            "faculties": impact.affected.faculties,
            "rooms": impact.affected.rooms,
            "sessions": impact.affected.sessions,
        },
        "new_conflicts": len(impact.new_conflicts),
        "resolved_conflicts": len(impact.resolved_conflicts),
        # Per tier, never summed -- ADR-0010.
        "score_delta": list(impact.score_delta),
        "tier_codes": list(impact.tier_codes),
        "stability_delta": impact.stability_delta,
        "summary": impact.summary(),
        "scope": assessment.scope.value,
        "scope_rationale": assessment.rationale,
        # The ids, not only the counts. Approval routing needs to know *which*
        # programmes a change reaches to ask their coordinators rather than
        # every coordinator in the institution.
        "affected_program_ids": list(impact.affected_program_ids),
        "affected_faculty_ids": list(impact.affected_faculty_ids),
    }


def _workflow_id_for(session: Session, code: str) -> uuid.UUID:
    from academic_ai.infrastructure.db.models import ApprovalWorkflow

    workflow = session.scalars(
        select(ApprovalWorkflow).where(ApprovalWorkflow.code == code)
    ).first()
    if workflow is None:  # pragma: no cover - the chain came from this table
        raise ChangeRequestError(f"approval workflow {code} disappeared")
    return workflow.id


def _next_code(session: Session, model, prefix: str) -> str:
    """A short human-facing code. Sequential per table.

    Referenced in conversation -- "have you looked at CR-14?" -- so it is
    short and stable rather than a UUID nobody can read aloud.
    """
    count = int(session.scalar(select(func.count()).select_from(model)) or 0)
    return f"{prefix}-{count + 1:04d}"


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
