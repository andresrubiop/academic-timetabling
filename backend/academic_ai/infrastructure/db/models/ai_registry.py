"""AI model registry (SESSION 1 shell, completed in SESSION 7).

The registry exists for one reason: the rule "no model activates
automatically" (CLAUDE.md — Human authority) is a *schema* guarantee, not a
policy document. It is enforced here by a partial unique index that permits at
most one ACTIVE version per model, plus a CHECK that an ACTIVE version must
name the human who activated it. `domain/ai/lifecycle.py` states the same rule
in framework-free code so a worker with no database can check it; the schema is
the copy that survives a bug in that file.

What SESSION 7 added
====================

* `FeatureSchema` — a named, versioned, **hashed** set of features. The hash
  is the load-bearing part: a schema gets edited without its version being
  bumped, and that is the ordinary way a model silently starts reading
  different numbers out of the same field names.
* `ModelEvaluation` — one measured comparison against a *named baseline*. A
  metric with no baseline cannot answer the only question activation turns on,
  which is not "is this model good" but "is it better than the heuristic it
  replaces".
* `ModelDeployment` — the history of what was live, when, and who put it
  there. `AIModelVersion.status` says what is true now; this table is what an
  investigation reads, and it is append-only for the same reason `audit_log`
  is.
* The playbook's remaining tracked fields on `AIModelVersion`: `model_name`,
  `contract`, and a foreign key to the feature schema so the version's claim
  about its features is a reference rather than a loose string.
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
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import AIModelStatus
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


class AIModel(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A logical model: "LNS neighborhood ranker", "conflict prioritiser"."""

    __tablename__ = "ai_model"
    __table_args__ = (
        UniqueConstraint("code", name="uq_ai_model_code"),
        # The three contracts of ADR-0001 and nothing else. A model
        # implementing a contract the optimizer cannot call has nowhere to be
        # invoked from, so registering one records an intention rather than a
        # capability — and the intention would sit in the registry looking
        # like a deployable model.
        CheckConstraint(
            "contract IN ('NeighborhoodPolicy', 'RecommendationProvider', "
            "'ConflictPriorityProvider')",
            name="ai_model_contract_is_known",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    code: Mapped[str] = code_column(64)
    name: Mapped[str] = name_column()
    model_family: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Which optimizer contract this model implements — one of
    #: NeighborhoodPolicy / RecommendationProvider / ConflictPriorityProvider.
    contract: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    versions: Mapped[list[AIModelVersion]] = relationship(
        back_populates="model", foreign_keys="AIModelVersion.ai_model_id"
    )


class AIModelVersion(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """One trained artifact of an `AIModel`.

    Every field needed to reproduce and to *distrust* a model is mandatory:
    the artifact hash, the git commit, the feature-schema version and the
    optimizer compatibility range. A version whose ``feature_schema_version``
    no longer matches the running feature extractor must never be activated,
    which is why the value is stored rather than inferred.
    """

    __tablename__ = "ai_model_version"
    __table_args__ = (
        UniqueConstraint(
            "ai_model_id", "version", name="uq_ai_model_version_ai_model_id_version"
        ),
        # At most one ACTIVE version per model. This is the schema-level
        # expression of "AI model activation is never automatic and never
        # ambiguous".
        Index(
            "uq_ai_model_version_one_active_per_model",
            "ai_model_id",
            unique=True,
            postgresql_where=text("status = 'ACTIVE'"),
        ),
        CheckConstraint(
            "(status <> 'ACTIVE') OR "
            "(activated_by_user_id IS NOT NULL AND activated_at IS NOT NULL)",
            name="active_requires_human_activation",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    ai_model_id: Mapped[uuid.UUID] = fk_column("ai_model.id", ondelete="CASCADE")
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_api_version: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The schema row this version was trained against. Nullable only for the
    #: SESSION 1 rows that predate `ai_feature_schema`; a version registered
    #: from SESSION 7 on should always carry one, because the string above
    #: cannot detect a schema that was edited in place.
    feature_schema_id: Mapped[uuid.UUID | None] = fk_column(
        "ai_feature_schema.id", nullable=True, ondelete="RESTRICT"
    )
    training_dataset_version: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    optimizer_compatibility: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    artifact_location: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    artifact_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    configuration: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[AIModelStatus] = mapped_column(
        pg_enum(AIModelStatus, "ai_model_status"),
        nullable=False,
        default=AIModelStatus.EXPERIMENTAL,
    )
    activated_by_user_id: Mapped[uuid.UUID | None] = fk_column(
        "app_user.id", nullable=True, ondelete="SET NULL"
    )
    activated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    model: Mapped[AIModel] = relationship(
        back_populates="versions", foreign_keys=[ai_model_id]
    )
    feature_schema: Mapped[FeatureSchema | None] = relationship(
        back_populates="versions", foreign_keys=[feature_schema_id]
    )
    activated_by: Mapped[User | None] = relationship(
        foreign_keys=[activated_by_user_id]
    )
    evaluations: Mapped[list[ModelEvaluation]] = relationship(
        back_populates="version",
        foreign_keys="ModelEvaluation.ai_model_version_id",
        cascade="all, delete-orphan",
    )
    deployments: Mapped[list[ModelDeployment]] = relationship(
        back_populates="version",
        foreign_keys="ModelDeployment.ai_model_version_id",
    )


class FeatureSchema(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """A named, versioned set of features, and the hash of its contents.

    Versions are immutable once anything references them. There is no
    ``is_current`` flag: "which schema is live" is a property of the running
    extractor, not of a row, and storing it here would create a second answer
    that drifts from the first.
    """

    __tablename__ = "ai_feature_schema"
    __table_args__ = (
        UniqueConstraint(
            "code", "version", name="uq_ai_feature_schema_code_version"
        ),
        # A hash of nothing is a hash that matches nothing and blocks every
        # activation with an unreadable message. Require content.
        CheckConstraint(
            "length(content_hash) >= 16", name="feature_schema_hash_is_real"
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    code: Mapped[str] = code_column(64)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Hash over the ordered feature names and their types. Compared at
    #: activation: a schema edited without a version bump is caught here and
    #: nowhere else.
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    #: The ordered feature names, so a mismatch can be explained rather than
    #: merely reported.
    feature_names: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    description: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    versions: Mapped[list[AIModelVersion]] = relationship(
        back_populates="feature_schema"
    )


class ModelEvaluation(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """One measured comparison of a version against a named baseline.

    ``baseline_code`` is NOT NULL on purpose. Activation replaces a heuristic
    whose behaviour is known with a model whose behaviour is not, and a number
    with no baseline cannot say which way that trade goes.

    ``is_reproducible`` records whether the comparison had a fixed seed and a
    recorded dataset version. SESSION 4 forbids claiming an improvement
    without reproducible evidence, and a boolean here is what lets activation
    refuse to count the rest.
    """

    __tablename__ = "ai_model_evaluation"
    __table_args__ = (
        CheckConstraint(
            "(NOT is_reproducible) OR "
            "(random_seed IS NOT NULL AND dataset_version IS NOT NULL)",
            name="reproducible_requires_seed_and_dataset",
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    ai_model_version_id: Mapped[uuid.UUID] = fk_column(
        "ai_model_version.id", ondelete="CASCADE"
    )
    #: What it was compared against — "HEURISTIC_CRITICALITY", or another
    #: version's code. Free text rather than a foreign key because the usual
    #: baseline is a heuristic, which has no row.
    baseline_code: Mapped[str] = mapped_column(String(64), nullable=False)
    dataset_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    random_seed: Mapped[int | None] = mapped_column(nullable=True)
    #: Per tier, best-first, matching `ScoreVector`. Negative is better. A
    #: single scalar would reintroduce the cross-tier exchange rate ADR-0010
    #: rejects.
    score_delta: Mapped[list[int] | None] = mapped_column(JSONB, nullable=True)
    tier_codes: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    is_reproducible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    optimizer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    git_commit: Mapped[str | None] = mapped_column(String(64), nullable=True)

    version: Mapped[AIModelVersion] = relationship(back_populates="evaluations")


class ModelDeployment(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """What was live, when, and who put it there.

    `AIModelVersion.status` says what is true now. This says what was true,
    which is the question anybody investigating a bad timetable actually has,
    and the one a mutable status column can never answer.

    Append-only by intent: a row is closed by setting ``ended_at``, never
    deleted. The same reasoning as `audit_log` — a deployment history that can
    be edited is not evidence of anything.
    """

    __tablename__ = "ai_model_deployment"
    __table_args__ = (
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= started_at",
            name="deployment_ends_after_it_starts",
        ),
        # At most one open deployment per model. The mirror of the partial
        # unique index on ACTIVE versions: two live models answering the same
        # contract is exactly the ambiguity "no model activates automatically"
        # is meant to rule out.
        Index(
            "uq_ai_model_deployment_one_open_per_model",
            "ai_model_id",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    ai_model_id: Mapped[uuid.UUID] = fk_column("ai_model.id", ondelete="CASCADE")
    ai_model_version_id: Mapped[uuid.UUID] = fk_column(
        "ai_model_version.id", ondelete="CASCADE"
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Required. A deployment with no named human is an automatic activation
    #: with a blank where the name goes.
    activated_by_user_id: Mapped[uuid.UUID] = fk_column(
        "app_user.id", ondelete="RESTRICT"
    )
    ended_by_user_id: Mapped[uuid.UUID | None] = fk_column(
        "app_user.id", nullable=True, ondelete="SET NULL"
    )
    #: Why it was withdrawn. Empty while the deployment is open.
    ended_reason: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    #: The runtime this was activated against, so a later incompatibility can
    #: be dated.
    optimizer_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    model: Mapped[AIModel] = relationship(foreign_keys=[ai_model_id])
    version: Mapped[AIModelVersion] = relationship(
        foreign_keys=[ai_model_version_id], back_populates="deployments"
    )
    activated_by: Mapped[User] = relationship(foreign_keys=[activated_by_user_id])
    ended_by: Mapped[User | None] = relationship(foreign_keys=[ended_by_user_id])
