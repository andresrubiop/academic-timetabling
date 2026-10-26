"""Change requests and approvals (foundational shells; SESSION 12 completes).

The one thing fixed now is that **no single universal approval chain is
hardcoded**. Within-programme, cross-programme and cross-faculty changes go
to different authorities, and institutions differ; so a workflow is data
(`ApprovalWorkflow` + ordered `ApprovalStep`s) and an approval records which
step it satisfied.

Every change request carries an impact analysis. A request without one cannot
be approved, because approving a schedule change without knowing whom it
affects is precisely the failure this platform exists to prevent.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import (
    ApprovalAction,
    ChangeRequestStatus,
    ScheduleScope,
    UserRole,
)
from academic_ai.infrastructure.db.base import (
    ActorMixin,
    Base,
    MetadataMixin,
    TimestampMixin,
    code_column,
    fk_column,
    name_column,
    pg_enum,
    pk_column,
)

if TYPE_CHECKING:
    from academic_ai.infrastructure.db.models.people import User


class ApprovalWorkflow(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A configurable chain of approval steps.

    ``applies_to_scope`` selects the workflow for a request: a change
    confined to one programme is not routed like one that moves a shared
    course across faculties.
    """

    __tablename__ = "approval_workflow"
    __table_args__ = (
        UniqueConstraint(
            "institution_id", "code", name="uq_approval_workflow_institution_id_code"
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    institution_id: Mapped[uuid.UUID] = fk_column("institution.id")
    code: Mapped[str] = code_column(48)
    name: Mapped[str] = name_column()
    applies_to_scope: Mapped[ScheduleScope] = mapped_column(
        pg_enum(ScheduleScope, "schedule_scope"), nullable=False
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true"
    )

    steps: Mapped[list[ApprovalStep]] = relationship(
        back_populates="workflow",
        cascade="all, delete-orphan",
        order_by="ApprovalStep.step_number",
    )


class ApprovalStep(Base, TimestampMixin):
    """One ordered step of a workflow, satisfied by a role.

    ``requires_all_affected`` covers the cross-programme case from the
    playbook: every affected coordinator must approve, not merely one of
    them.
    """

    __tablename__ = "approval_step"
    __table_args__ = (
        UniqueConstraint(
            "approval_workflow_id",
            "step_number",
            name="uq_approval_step_workflow_id_step_number",
        ),
        CheckConstraint("step_number > 0", name="step_number_positive"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    approval_workflow_id: Mapped[uuid.UUID] = fk_column(
        "approval_workflow.id", ondelete="CASCADE"
    )
    step_number: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    required_role: Mapped[UserRole] = mapped_column(
        pg_enum(UserRole, "user_role"), nullable=False
    )
    #: When true, every affected holder of `required_role` must approve.
    requires_all_affected: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    workflow: Mapped[ApprovalWorkflow] = relationship(back_populates="steps")


class ChangeRequest(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A proposed change to an accepted schedule.

    Originates from a scenario (SESSION 10/11) or from a recommendation
    (SESSION 6). ``impact`` is NOT NULL by construction: the request cannot
    be created without it.
    """

    __tablename__ = "change_request"
    __table_args__ = (
        UniqueConstraint(
            "academic_period_id", "code", name="uq_change_request_period_code"
        ),
        CheckConstraint(
            "(status <> 'APPLIED') OR applied_schedule_version_id IS NOT NULL",
            name="applied_requires_resulting_version",
        ),
        # The playbook: every request requires ImpactAnalysis. NOT NULL is not
        # enough, because the column defaults to '{}' -- a request approved on
        # no evidence would look identical to one approved on plenty. A DRAFT
        # is exempt: it is a form somebody is still filling in.
        CheckConstraint(
            "status = 'DRAFT' OR impact <> '{}'::jsonb",
            name="submitted_request_carries_an_impact_analysis",
        ),
        Index(
            "ix_change_request_academic_period_id_status",
            "academic_period_id",
            "status",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    academic_period_id: Mapped[uuid.UUID] = fk_column("academic_period.id")
    code: Mapped[str] = code_column(48)
    title: Mapped[str] = name_column()
    description: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    requested_by_user_id: Mapped[uuid.UUID] = fk_column("app_user.id")
    approval_workflow_id: Mapped[uuid.UUID] = fk_column("approval_workflow.id")
    base_schedule_version_id: Mapped[uuid.UUID] = fk_column("schedule_version.id")
    scenario_id: Mapped[uuid.UUID | None] = fk_column(
        "scenario.id", nullable=True, ondelete="SET NULL"
    )
    recommendation_id: Mapped[uuid.UUID | None] = fk_column(
        "recommendation.id", nullable=True, ondelete="SET NULL"
    )
    applied_schedule_version_id: Mapped[uuid.UUID | None] = fk_column(
        "schedule_version.id", nullable=True, ondelete="SET NULL"
    )
    status: Mapped[ChangeRequestStatus] = mapped_column(
        pg_enum(ChangeRequestStatus, "change_request_status"),
        nullable=False,
        default=ChangeRequestStatus.DRAFT,
    )
    #: Affected students, teachers, programmes, faculties, rooms, new and
    #: resolved conflicts, score delta, stability delta.
    #: The measured impact. NOT NULL was never enough: the column defaults to
    #: '{}', so a request could be approved on no evidence and look identical
    #: to one approved on plenty. The CHECK below is what makes the playbook's
    #: "every request requires ImpactAnalysis" true of the data rather than of
    #: the code that usually writes it.
    impact: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )

    requested_by: Mapped[User] = relationship(foreign_keys=[requested_by_user_id])
    workflow: Mapped[ApprovalWorkflow] = relationship()
    approvals: Mapped[list[Approval]] = relationship(
        back_populates="change_request", cascade="all, delete-orphan"
    )


class Approval(Base, TimestampMixin):
    """One actor's decision on one step of a change request.

    A counterproposal is an action here rather than a separate entity: it
    carries ``counterproposal`` describing the alternative, and the
    application turns an accepted counterproposal into a new
    `ChangeRequest`, so every decision stays on one auditable timeline.
    """

    __tablename__ = "approval"
    __table_args__ = (
        UniqueConstraint(
            "change_request_id",
            "approval_step_id",
            "actor_user_id",
            name="uq_approval_request_step_actor",
        ),
        CheckConstraint(
            "(action <> 'COUNTERPROPOSE') OR counterproposal IS NOT NULL",
            name="counterproposal_requires_payload",
        ),
        CheckConstraint(
            "(action <> 'REJECT') OR comment IS NOT NULL",
            name="rejection_requires_comment",
        ),
        Index("ix_approval_change_request_id", "change_request_id"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    change_request_id: Mapped[uuid.UUID] = fk_column(
        "change_request.id", ondelete="CASCADE", index=False
    )
    approval_step_id: Mapped[uuid.UUID] = fk_column("approval_step.id")
    actor_user_id: Mapped[uuid.UUID] = fk_column("app_user.id")
    action: Mapped[ApprovalAction] = mapped_column(
        pg_enum(ApprovalAction, "approval_action"), nullable=False
    )
    comment: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    counterproposal: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )

    change_request: Mapped[ChangeRequest] = relationship(back_populates="approvals")
    step: Mapped[ApprovalStep] = relationship()
    actor: Mapped[User] = relationship(foreign_keys=[actor_user_id])


class ReoptimizationRequest(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A reviewer asking for the optimizer to try again (SESSION 12).

    `REQUEST_REOPTIMIZATION` is one of the five approval actions, and it is
    the only one that produces work rather than a decision. It gets its own
    table because it has a lifecycle the `approval` row does not: it is
    submitted, a run is created for it, that run finishes, and somebody reads
    the result. Recording it as a comment on an approval would lose the link
    between "the dean asked for this" and the `optimization_run` that answered.

    It never applies anything. The run produces a scenario; adopting the
    scenario is a change request like any other.
    """

    __tablename__ = "reoptimization_request"
    __table_args__ = (
        # A reoptimization is asked for by a person reviewing something. One
        # with no reason cannot be prioritised against the others in the queue.
        CheckConstraint(
            "length(btrim(reason)) > 0", name="reoptimization_states_a_reason"
        ),
        Index("ix_reoptimization_request_change_request", "change_request_id"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    change_request_id: Mapped[uuid.UUID] = fk_column(
        "change_request.id", ondelete="CASCADE"
    )
    requested_by_user_id: Mapped[uuid.UUID] = fk_column("app_user.id")
    reason: Mapped[str] = mapped_column(String(2048), nullable=False)
    #: What the reviewer wants held still. Free-form because "keep Tuesday as
    #: it is" is the shape of the request, and turning that into constraints
    #: is the optimizer's job, not the form's.
    constraints_hint: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    #: The run created to answer it. NULL until one is queued -- the request
    #: exists the moment it is asked, not the moment it is served.
    optimization_run_id: Mapped[uuid.UUID | None] = fk_column(
        "optimization_run.id", nullable=True, ondelete="SET NULL"
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    change_request: Mapped[ChangeRequest] = relationship(
        foreign_keys=[change_request_id]
    )
    requested_by: Mapped[User] = relationship(foreign_keys=[requested_by_user_id])
