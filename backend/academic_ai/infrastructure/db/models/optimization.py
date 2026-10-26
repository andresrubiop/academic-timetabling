"""Optimization runs, metrics and recommendations (foundational shells).

SESSION 3 and 4 implement the solver; this module fixes the record they must
leave behind.

The design point that matters now is **input snapshotting**. Constraints,
weights, availability and enrollment all mutate continuously. A run that
merely referenced them would be unreproducible the moment anything changed —
and the playbook forbids claiming an LNS improvement "without reproducible
evidence" (SESSION 4) and requires reproducible fixtures (SESSION 3). So a
run stores the *resolved effective constraint set*, an input-data hash, the
solver seed and parameters, and the git commit. Cheap now; unreconstructible
later.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import (
    OptimizationRunStatus,
    RecommendationKind,
    RecommendationStatus,
    SolverStatus,
)
from academic_ai.infrastructure.db.base import (
    ActorMixin,
    Base,
    MetadataMixin,
    TimestampMixin,
    UUIDType,
    fk_column,
    pg_enum,
    pk_column,
)

if TYPE_CHECKING:
    from academic_ai.infrastructure.db.models.people import User


class OptimizationRun(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """One execution of the optimizer.

    ``status`` is *progress*; ``solver_status`` is the *outcome*. They are
    separate columns because conflating them destroys the distinction the
    playbook insists on: INFEASIBLE (proven: no schedule exists) is a
    completely different fact from UNKNOWN or TIMEOUT (the search gave up),
    and telling a coordinator "no solution" when the truth is "we stopped
    looking after 60 seconds" is the failure SESSION 6 exists to prevent.
    """

    __tablename__ = "optimization_run"
    __table_args__ = (
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="finished_after_started",
        ),
        # A finished run must say what the solver concluded.
        CheckConstraint(
            "status NOT IN ('COMPLETED', 'FAILED') OR solver_status IS NOT NULL",
            name="terminal_run_has_solver_status",
        ),
        Index(
            "ix_optimization_run_academic_period_id_status",
            "academic_period_id",
            "status",
        ),
        CheckConstraint(
            "progress_percent BETWEEN 0 AND 100", name="progress_percent_in_range"
        ),
        # A cancellation is an action somebody took. Recording the effect
        # without the actor produces an audit trail that says a run stopped
        # and cannot say why.
        CheckConstraint(
            "cancel_requested_at IS NULL OR cancel_requested_by_user_id IS NOT NULL",
            name="cancellation_names_its_requester",
        ),
        # Runs a worker has claimed, oldest heartbeat first: the query the
        # reaper makes to find runs whose worker died.
        Index(
            "ix_optimization_run_heartbeat",
            "heartbeat_at",
            postgresql_where=text(
                "status IN ('VALIDATING','BUILDING_MODEL','SEARCHING_FEASIBLE',"
                "'FEASIBLE_FOUND','OPTIMIZING','EVALUATING')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    academic_period_id: Mapped[uuid.UUID] = fk_column("academic_period.id")
    #: The version this run started from (NULL for a from-scratch build).
    base_schedule_version_id: Mapped[uuid.UUID | None] = fk_column(
        "schedule_version.id", nullable=True, ondelete="SET NULL"
    )
    scenario_id: Mapped[uuid.UUID | None] = fk_column(
        "scenario.id", nullable=True, ondelete="SET NULL"
    )
    status: Mapped[OptimizationRunStatus] = mapped_column(
        pg_enum(OptimizationRunStatus, "optimization_run_status"),
        nullable=False,
        default=OptimizationRunStatus.QUEUED,
    )
    solver_status: Mapped[SolverStatus | None] = mapped_column(
        pg_enum(SolverStatus, "solver_status"), nullable=True
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    finished_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # --- reproducibility snapshot ---
    #: The resolved, effective constraint set actually used by this run,
    #: including each constraint type's parameter_schema_version.
    effective_constraints: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    #: Hash over the input data (offerings, sessions, enrollments,
    #: availability, rooms) as loaded. Two runs with the same hash and the
    #: same seed must produce the same result.
    input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    solver_parameters: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True
    )
    random_seed: Mapped[int | None] = mapped_column(Integer, nullable=True)
    git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Which AI model version, if any, guided the search. NULL means pure
    #: CP-SAT / heuristic LNS. AI never changes feasibility, only search.
    ai_model_version_id: Mapped[uuid.UUID | None] = fk_column(
        "ai_model_version.id", nullable=True, ondelete="SET NULL"
    )

    objective_value: Mapped[float | None] = mapped_column(
        Numeric(18, 6), nullable=True
    )
    failure_reason: Mapped[str | None] = mapped_column(String(4096), nullable=True)

    # --- SESSION 9: queue, progress, cancellation ---------------------------
    #: 0..100. Coarse and honest: it is derived from which phase the run is
    #: in, not from CP-SAT's internal progress, because CP-SAT cannot say how
    #: close it is to proving optimality and a percentage that invents one
    #: would be a lie the UI renders as a smooth bar.
    progress_percent: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: What the run is doing, in words a coordinator can read.
    progress_message: Mapped[str | None] = mapped_column(String(512), nullable=True)

    #: Set when somebody asks for cancellation. Cancellation is *cooperative*:
    #: the worker's CP-SAT callback polls this and calls StopSearch(), so the
    #: run stops at a consistent point and keeps the best solution it had.
    #: Killing the process instead would lose that solution and leave the row
    #: claiming OPTIMIZING forever.
    cancel_requested_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_requested_by_user_id: Mapped[uuid.UUID | None] = fk_column(
        "app_user.id", nullable=True, ondelete="SET NULL"
    )

    #: Which worker holds this run, and when it last said so. A run whose
    #: heartbeat has gone stale was claimed by a worker that died; without
    #: these two columns such a run sits in OPTIMIZING forever and nobody can
    #: tell it apart from one that is simply slow.
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    heartbeat_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: How many times this run has been handed to a worker. Bounded retries:
    #: a run that crashes its worker will crash the next one, and an unbounded
    #: retry turns one bad instance into an outage for every queued run behind
    #: it.
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )

    metrics: Mapped[list[OptimizationMetric]] = relationship(
        back_populates="optimization_run", cascade="all, delete-orphan"
    )


class OptimizationMetric(Base, TimestampMixin):
    """One named measurement produced by a run.

    Structured rows rather than a JSON blob so benchmarks across runs are a
    query, not a parsing exercise.
    """

    __tablename__ = "optimization_metric"
    __table_args__ = (
        UniqueConstraint(
            "optimization_run_id",
            "name",
            "iteration",
            name="uq_optimization_metric_run_name_iteration",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_optimization_metric_name", "name"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    optimization_run_id: Mapped[uuid.UUID] = fk_column(
        "optimization_run.id", ondelete="CASCADE"
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[float] = mapped_column(Numeric(18, 6), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(32), nullable=True)
    #: NULL for run-level metrics; set for per-LNS-iteration series.
    iteration: Mapped[int | None] = mapped_column(Integer, nullable=True)

    optimization_run: Mapped[OptimizationRun] = relationship(back_populates="metrics")


class Recommendation(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A proposed change, with structured evidence for its impact.

    ``impact`` is mandatory: the playbook requires every recommendation to
    carry structured impact evidence, and a recommendation without it is an
    opinion. ``rationale`` is the human-readable explanation that makes the
    recommendation explainable rather than oracular.

    A recommendation is never self-applying. Acting on one produces a
    `ChangeRequest`, which follows the approval workflow like any other
    change.
    """

    __tablename__ = "recommendation"
    __table_args__ = (
        Index(
            "ix_recommendation_academic_period_id_status",
            "academic_period_id",
            "status",
        ),
        Index("ix_recommendation_subject_type_subject_id", "subject_type", "subject_id"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    academic_period_id: Mapped[uuid.UUID] = fk_column("academic_period.id")
    optimization_run_id: Mapped[uuid.UUID | None] = fk_column(
        "optimization_run.id", nullable=True, ondelete="CASCADE"
    )
    schedule_version_id: Mapped[uuid.UUID | None] = fk_column(
        "schedule_version.id", nullable=True, ondelete="CASCADE"
    )
    ai_model_version_id: Mapped[uuid.UUID | None] = fk_column(
        "ai_model_version.id", nullable=True, ondelete="SET NULL"
    )
    kind: Mapped[RecommendationKind] = mapped_column(
        pg_enum(RecommendationKind, "recommendation_kind"), nullable=False
    )
    status: Mapped[RecommendationStatus] = mapped_column(
        pg_enum(RecommendationStatus, "recommendation_status"),
        nullable=False,
        default=RecommendationStatus.PROPOSED,
    )
    subject_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    #: Structured evidence: affected students, teachers, programmes,
    #: faculties, rooms, new/resolved conflicts, score delta, stability delta.
    impact: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict
    )
    rationale: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Numeric(5, 4), nullable=True)
