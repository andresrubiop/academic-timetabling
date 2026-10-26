"""Notification rules (SESSION 17).

Framework-free: who should be told what, and what should *not* be sent, are
decisions that need no database and no mail server.
"""

from academic_ai.domain.notifications.rules import (
    ACTIONABLE_KINDS,
    KIND_RULES,
    NotificationRequest,
    NotificationRule,
    coalesce,
    rule_for,
    suppress_actor,
)

__all__ = [
    "ACTIONABLE_KINDS",
    "KIND_RULES",
    "NotificationRequest",
    "NotificationRule",
    "coalesce",
    "rule_for",
    "suppress_actor",
]
