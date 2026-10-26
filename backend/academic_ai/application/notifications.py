"""Delivering notifications, and refusing to pretend when a channel cannot.

Three channels are declared -- WEB, EMAIL, PUSH -- and only WEB is
implemented, which the playbook says is enough for now. The design decision
worth stating is what the other two do in the meantime.

**An unimplemented channel refuses; it does not quietly drop.** A `send()`
that returned success and did nothing would be the worst possible
placeholder: somebody would configure email notifications, see no errors, and
believe deans were being told about approvals for weeks. So EMAIL and PUSH
report themselves unavailable, the dispatcher records the failure, and a
caller asking for them gets an answer rather than silence.

**Delivery never fails the thing that triggered it.** An optimization run that
finished must not be marked failed because a notification could not be
written. `dispatch` collects failures and returns them; it does not raise.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, runtime_checkable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from academic_ai.domain.enums import NotificationChannel, NotificationKind
from academic_ai.domain.notifications import (
    NotificationRequest,
    coalesce,
    rule_for,
    suppress_actor,
)

__all__ = [
    "ChannelUnavailable",
    "DeliveryReport",
    "EmailChannel",
    "NotificationChannelPort",
    "PushChannel",
    "WebChannel",
    "channel_for",
    "dispatch",
    "mark_read",
    "unread_count",
]

log = logging.getLogger("academic_ai.notifications")


class ChannelUnavailable(RuntimeError):
    """This channel is declared but not implemented on this deployment."""


@runtime_checkable
class NotificationChannelPort(Protocol):
    """How a notification reaches somebody.

    Deliberately narrow: a channel delivers one already-decided notification.
    It does not choose recipients, suppress duplicates, or decide whether the
    news is worth sending -- those are the domain's, and a channel that made
    them would make each new channel a place for the rules to drift.
    """

    channel: NotificationChannel
    is_available: bool

    def deliver(self, session: Session, request: NotificationRequest) -> None: ...


@dataclass(frozen=True, slots=True)
class WebChannel:
    """In-app. Writes a `notification` row and nothing else."""

    channel: NotificationChannel = NotificationChannel.WEB
    is_available: bool = True

    def deliver(self, session: Session, request: NotificationRequest) -> None:
        from academic_ai.infrastructure.db.models import Notification

        session.add(
            Notification(
                recipient_user_id=uuid.UUID(request.recipient_user_id),
                kind=request.kind,
                channel=NotificationChannel.WEB,
                title=request.title[:255],
                body=request.body[:4096] or None,
                payload=dict(request.payload) or None,
                subject_type=request.subject_type,
                subject_id=(
                    uuid.UUID(request.subject_id) if request.subject_id else None
                ),
                is_actionable=request.is_actionable,
            )
        )


@dataclass(frozen=True, slots=True)
class EmailChannel:
    """Declared, not implemented.

    Refuses rather than silently succeeding. A channel that returned success
    and did nothing would let somebody configure email, see no errors, and
    believe deans had been told about approvals for weeks.
    """

    channel: NotificationChannel = NotificationChannel.EMAIL
    is_available: bool = False

    def deliver(self, session: Session, request: NotificationRequest) -> None:
        raise ChannelUnavailable(
            "email delivery is not configured on this deployment; the "
            "notification was not sent. It is still visible in the "
            "recipient's in-app list."
        )


@dataclass(frozen=True, slots=True)
class PushChannel:
    """Declared, not implemented. Same reasoning as `EmailChannel`."""

    channel: NotificationChannel = NotificationChannel.PUSH
    is_available: bool = False

    def deliver(self, session: Session, request: NotificationRequest) -> None:
        raise ChannelUnavailable(
            "push delivery is not configured on this deployment; the "
            "notification was not sent. It is still visible in the "
            "recipient's in-app list."
        )


_CHANNELS: Mapping[NotificationChannel, NotificationChannelPort] = {
    NotificationChannel.WEB: WebChannel(),
    NotificationChannel.EMAIL: EmailChannel(),
    NotificationChannel.PUSH: PushChannel(),
}


def channel_for(channel: NotificationChannel) -> NotificationChannelPort:
    port = _CHANNELS.get(channel)
    if port is None:  # pragma: no cover - the enum is closed
        raise KeyError(f"no port for channel {channel}")
    return port


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """What was sent, what was suppressed, and what could not be delivered."""

    requested: int = 0
    delivered: int = 0
    #: Dropped by the rules -- an actor told about their own action, or a
    #: duplicate collapsed into another. Not a failure; the point of the rules.
    suppressed: int = 0
    failures: tuple[str, ...] = ()

    @property
    def has_failures(self) -> bool:
        return bool(self.failures)

    def summary(self) -> str:
        parts = [f"{self.delivered} delivered"]
        if self.suppressed:
            parts.append(f"{self.suppressed} suppressed")
        if self.failures:
            parts.append(f"{len(self.failures)} undeliverable")
        return ", ".join(parts)


def dispatch(
    session: Session, requests: Sequence[NotificationRequest]
) -> DeliveryReport:
    """Apply the rules, then deliver what survives.

    Never raises for a delivery problem. An optimization run that finished
    must not be recorded as failed because a notification could not be
    written; failures come back in the report for the caller to log.
    """
    requested = len(requests)
    kept = coalesce(suppress_actor(requests))

    delivered = 0
    failures: list[str] = []
    for request in kept:
        port = channel_for(request.channel)
        try:
            port.deliver(session, request)
            delivered += 1
        except ChannelUnavailable as exc:
            failures.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - delivery must not propagate
            log.exception(
                "notification delivery failed",
                extra={
                    "kind": request.kind.value,
                    "channel": request.channel.value,
                },
            )
            failures.append(f"{request.kind.value}: {type(exc).__name__}")

    if delivered:
        session.flush()

    report = DeliveryReport(
        requested=requested,
        delivered=delivered,
        suppressed=requested - len(kept),
        failures=tuple(failures),
    )
    log.info("notifications dispatched", extra={"outcome": report.summary()})
    return report


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def unread_count(session: Session, user_id: str) -> int:
    from academic_ai.infrastructure.db.models import Notification

    return int(
        session.scalar(
            select(func.count())
            .select_from(Notification)
            .where(Notification.recipient_user_id == uuid.UUID(user_id))
            .where(Notification.read_at.is_(None))
        )
        or 0
    )


def mark_read(
    session: Session,
    user_id: str,
    notification_ids: Sequence[str] | None = None,
    *,
    now: dt.datetime | None = None,
) -> int:
    """Mark some or all of a user's notifications read.

    Scoped to the caller's own rows by the `recipient_user_id` filter, so an
    id belonging to somebody else simply matches nothing -- a 404 would
    confirm that the id exists.
    """
    from academic_ai.infrastructure.db.models import Notification

    now = now or dt.datetime.now(dt.timezone.utc)
    stmt = (
        update(Notification)
        .where(Notification.recipient_user_id == uuid.UUID(user_id))
        .where(Notification.read_at.is_(None))
    )
    if notification_ids is not None:
        stmt = stmt.where(
            Notification.id.in_([uuid.UUID(i) for i in notification_ids])
        )
    result = session.execute(stmt.values(read_at=now))
    session.flush()
    return int(result.rowcount or 0)
