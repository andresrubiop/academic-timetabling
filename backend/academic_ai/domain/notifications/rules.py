"""What notifies, who it reaches, and -- the harder half -- what does not.

A notification system's failure mode is not silence. It is noise. Every
approval step, every finished run and every imported row *could* produce one,
and a coordinator who receives forty a day stops reading them, at which point
the one that mattered is lost exactly as completely as if it had never been
sent. So the interesting content of this module is the suppressions.

Four rules, each of which exists because the obvious implementation gets it
wrong.

**Never notify somebody about their own action.** A director who approves a
request does not need to be told a request was approved; they are the one who
approved it. This is the single largest source of noise in a naive
implementation, because the actor is almost always in the set of interested
parties.

**One event, one notification per person.** A schedule change that moves forty
sessions must not send a student forty messages. `coalesce` collapses them
into one that says how many of *their* classes moved -- which is also the only
form in which the number means anything to them.

**Only tell people who can act, when the point is acting.**
`APPROVAL_REQUIRED` goes to whoever the chain is actually waiting on, not to
everybody in the chain. Telling the third approver that a request exists,
seven days before it reaches them, trains them to ignore the message that
eventually matters.

**A provisional schedule is not a published one.** They are separate kinds
because they are separate facts, and a student who was told once that their
timetable is ready should not have to guess whether this one is the real one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from academic_ai.domain.enums import NotificationChannel, NotificationKind, UserRole

__all__ = [
    "ACTIONABLE_KINDS",
    "KIND_RULES",
    "NotificationRequest",
    "NotificationRule",
    "coalesce",
    "rule_for",
    "suppress_actor",
]


@dataclass(frozen=True, slots=True)
class NotificationRule:
    """How one kind of event behaves."""

    kind: NotificationKind
    #: Whether receiving it means somebody is expected to do something. Drives
    #: ordering: a request waiting on you outranks a run that finished.
    is_actionable: bool
    #: Whether two of these about the same subject collapse into one. False
    #: only where each occurrence is genuinely separate news.
    coalesces: bool = True
    #: Notify the person whose action caused it. Almost always False -- they
    #: were there.
    notify_actor: bool = False
    description: str = ""


_K = NotificationKind

KIND_RULES: Mapping[NotificationKind, NotificationRule] = {
    _K.PROVISIONAL_SCHEDULE: NotificationRule(
        _K.PROVISIONAL_SCHEDULE, is_actionable=False,
        description=(
            "A draft timetable is available to look at. Deliberately distinct "
            "from PUBLISHED_SCHEDULE: a student told once that their timetable "
            "is ready should not have to guess whether this one is the real one."
        ),
    ),
    _K.PUBLISHED_SCHEDULE: NotificationRule(
        _K.PUBLISHED_SCHEDULE, is_actionable=False,
        description="The timetable is now in force.",
    ),
    _K.SCHEDULE_CHANGED: NotificationRule(
        _K.SCHEDULE_CHANGED, is_actionable=False, coalesces=True,
        description=(
            "Something moved in a published timetable. Coalesced hard: forty "
            "moved sessions must reach a student as one message saying how "
            "many of *their* classes moved, which is the only form in which "
            "the number means anything to them."
        ),
    ),
    _K.APPROVAL_REQUIRED: NotificationRule(
        _K.APPROVAL_REQUIRED, is_actionable=True, coalesces=False,
        description=(
            "A change request is waiting on you. Sent only to whoever the "
            "chain is actually waiting on -- telling the third approver seven "
            "days early trains them to ignore the message that matters."
        ),
    ),
    _K.REQUEST_APPROVED: NotificationRule(
        _K.REQUEST_APPROVED, is_actionable=False, coalesces=False,
        description="A request you raised was approved.",
    ),
    _K.REQUEST_REJECTED: NotificationRule(
        _K.REQUEST_REJECTED, is_actionable=True, coalesces=False,
        description=(
            "A request you raised was rejected. Actionable, because a "
            "rejection carries a reason and the requester is the one who can "
            "answer it."
        ),
    ),
    _K.COUNTERPROPOSAL: NotificationRule(
        _K.COUNTERPROPOSAL, is_actionable=True, coalesces=False,
        description=(
            "A reviewer proposed something different. Actionable: somebody is "
            "waiting on an answer, which is what makes it not a rejection."
        ),
    ),
    _K.CONFLICT_DETECTED: NotificationRule(
        _K.CONFLICT_DETECTED, is_actionable=True, coalesces=True,
        description=(
            "A clash appeared that affects you. Coalesced per person: a "
            "student with three clashes has one problem, not three."
        ),
    ),
    _K.AI_RECOMMENDATION: NotificationRule(
        _K.AI_RECOMMENDATION, is_actionable=True, coalesces=True,
        description=(
            "A model suggested something. Actionable because a recommendation "
            "is a proposal a human decides on -- it never self-applies "
            "(CLAUDE.md)."
        ),
    ),
    _K.OPTIMIZATION_COMPLETE: NotificationRule(
        _K.OPTIMIZATION_COMPLETE, is_actionable=False, coalesces=False,
        notify_actor=True,
        description=(
            "A run finished. The one kind that *does* notify its actor: they "
            "submitted it and then went away, which is the whole reason the "
            "run is asynchronous."
        ),
    ),
}

ACTIONABLE_KINDS = frozenset(
    kind for kind, rule in KIND_RULES.items() if rule.is_actionable
)


def rule_for(kind: NotificationKind) -> NotificationRule:
    rule = KIND_RULES.get(kind)
    if rule is None:  # pragma: no cover - the enum is closed
        raise KeyError(f"no notification rule for {kind}")
    return rule


@dataclass(frozen=True, slots=True)
class NotificationRequest:
    """One notification somebody is asking to send.

    Built by whoever detected the event; filtered and collapsed by the rules
    here before anything is written.
    """

    kind: NotificationKind
    recipient_user_id: str
    title: str
    body: str = ""
    channel: NotificationChannel = NotificationChannel.WEB
    subject_type: str | None = None
    subject_id: str | None = None
    payload: Mapping[str, object] = field(default_factory=dict)
    #: Who caused this. Used to suppress notifying them about themselves.
    actor_user_id: str | None = None

    @property
    def is_actionable(self) -> bool:
        return rule_for(self.kind).is_actionable

    @property
    def coalescing_key(self) -> tuple[str, str, str, str]:
        """What counts as "the same news" for one person."""
        return (
            self.recipient_user_id,
            self.kind.value,
            self.subject_type or "",
            self.subject_id or "",
        )


def suppress_actor(
    requests: Sequence[NotificationRequest],
) -> tuple[NotificationRequest, ...]:
    """Drop notifications that would tell somebody what they just did.

    The largest source of noise in a naive implementation, because the actor
    is almost always inside the set of interested parties -- the director who
    approves is in the approval chain, the coordinator who moves a class
    teaches on it.

    `OPTIMIZATION_COMPLETE` is the deliberate exception: the submitter started
    a job and went away, which is the entire reason it runs asynchronously.
    """
    return tuple(
        request
        for request in requests
        if rule_for(request.kind).notify_actor
        or request.actor_user_id is None
        or request.actor_user_id != request.recipient_user_id
    )


def coalesce(
    requests: Sequence[NotificationRequest],
) -> tuple[NotificationRequest, ...]:
    """Collapse repeated news about one subject for one person.

    A schedule change that moves forty sessions produces forty requests for a
    student enrolled in three of them. They get one, and its payload says
    three -- the number that is about *them*. Forty would be noise, and three
    separate messages would still be two too many.

    Non-coalescing kinds pass through untouched: two different change requests
    both waiting on the same director are two things to do, not one.
    """
    out: dict[tuple[str, str, str, str], NotificationRequest] = {}
    passthrough: list[NotificationRequest] = []

    for request in requests:
        if not rule_for(request.kind).coalesces:
            passthrough.append(request)
            continue

        key = request.coalescing_key
        existing = out.get(key)
        if existing is None:
            payload = dict(request.payload)
            payload.setdefault("occurrences", 1)
            out[key] = NotificationRequest(
                kind=request.kind,
                recipient_user_id=request.recipient_user_id,
                title=request.title,
                body=request.body,
                channel=request.channel,
                subject_type=request.subject_type,
                subject_id=request.subject_id,
                payload=payload,
                actor_user_id=request.actor_user_id,
            )
            continue

        payload = dict(existing.payload)
        payload["occurrences"] = int(payload.get("occurrences", 1)) + 1
        # Merge the detail lists the callers supplied, so "3 of your classes
        # moved" can name which three.
        for key_name in ("sessions", "courses", "items"):
            merged = list(existing.payload.get(key_name, ())) + list(
                request.payload.get(key_name, ())
            )
            if merged:
                payload[key_name] = merged
        out[key] = NotificationRequest(
            kind=existing.kind,
            recipient_user_id=existing.recipient_user_id,
            title=existing.title,
            body=existing.body,
            channel=existing.channel,
            subject_type=existing.subject_type,
            subject_id=existing.subject_id,
            payload=payload,
            actor_user_id=existing.actor_user_id,
        )

    return tuple(passthrough) + tuple(out.values())
