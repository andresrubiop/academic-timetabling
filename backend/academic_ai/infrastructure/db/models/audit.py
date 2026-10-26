"""Audit log and notifications.

Both target an open set of entity types, so they use ``(entity_type,
entity_id)`` string/UUID pairs with no foreign key. This is the one place
where losing referential integrity is the right trade: an audit row must
survive the deletion or retirement of whatever it describes, and adding a
new auditable entity must not require a migration.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Index, String, func
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.domain.enums import (
    AuditAction,
    NotificationChannel,
    NotificationKind,
)
from academic_ai.infrastructure.db.base import (
    Base,
    TimestampMixin,
    UUIDType,
    fk_column,
    pg_enum,
    pk_column,
)

if TYPE_CHECKING:
    from academic_ai.infrastructure.db.models.people import User


class AuditLog(Base):
    """Append-only record of who changed what.

    Deliberately does NOT use `TimestampMixin`: an audit row has one moment
    (``occurred_at``) and must never carry an ``updated_at``, because it must
    never be updated. SESSION 8 revokes UPDATE and DELETE on this table from
    the application database role; until then the rule is enforced by review
    and by the absence of any update path in the codebase.

    ``before``/``after`` hold the changed fields only, not whole rows, to
    keep the table's growth proportional to real change rather than to row
    width.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity_type_entity_id", "entity_type", "entity_id"),
        Index("ix_audit_log_occurred_at", "occurred_at"),
        Index("ix_audit_log_actor_user_id_occurred_at", "actor_user_id", "occurred_at"),
        Index("ix_audit_log_correlation_id", "correlation_id"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    occurred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    actor_user_id: Mapped[uuid.UUID | None] = fk_column(
        "app_user.id", nullable=True, ondelete="SET NULL", index=False
    )
    #: NULL actor means the change came from a migration, an automated
    #: import, or the optimizer worker. ``actor_description`` says which.
    actor_description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[AuditAction] = mapped_column(
        pg_enum(AuditAction, "audit_action"), nullable=False
    )
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Ties together every row produced by one request or one optimizer run.
    correlation_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    actor: Mapped[User | None] = relationship()


class Notification(Base, TimestampMixin):
    """An in-app notification. WEB is the only channel implemented in
    SESSION 17; the channel column exists now so adding EMAIL/PUSH is a
    delivery concern rather than a schema migration."""

    __tablename__ = "notification"
    __table_args__ = (
        Index(
            "ix_notification_recipient_user_id_read_at",
            "recipient_user_id",
            "read_at",
        ),
        Index("ix_notification_subject_type_subject_id", "subject_type", "subject_id"),
    )

    id: Mapped[uuid.UUID] = pk_column()
    recipient_user_id: Mapped[uuid.UUID] = fk_column(
        "app_user.id", ondelete="CASCADE", index=False
    )
    kind: Mapped[NotificationKind] = mapped_column(
        pg_enum(NotificationKind, "notification_kind"), nullable=False
    )
    channel: Mapped[NotificationChannel] = mapped_column(
        pg_enum(NotificationChannel, "notification_channel"),
        nullable=False,
        default=NotificationChannel.WEB,
    )
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    subject_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
    read_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_actionable: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )

    recipient: Mapped[User] = relationship()
