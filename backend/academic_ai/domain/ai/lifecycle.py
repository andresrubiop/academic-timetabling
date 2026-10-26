"""What may happen to a model version, and who has to be standing there.

The playbook lists six statuses and eight actions. Written as a table rather
than as a chain of ``if`` statements, because the interesting property is not
any single transition — it is that **no path reaches ACTIVE without a named
human**, and a table can be read to check that in one sitting.

Three rules the types enforce
=============================

**ACTIVATE requires an actor, always.** `apply_action` refuses without one.
CLAUDE.md puts AI model activation in the same category as academic
equivalence and cross-faculty changes: things an AI recommendation never
automatically approves. A default of "the system" would be that approval,
spelled differently.

**A version reaches ACTIVE only from READY.** READY is the state that says
somebody ran TEST and BENCHMARK and looked at the numbers. Allowing
EXPERIMENTAL -> ACTIVE would make the intervening states decorative, and the
first time anybody used the shortcut would be during an incident.

**ROLLBACK is a first-class action, not the absence of one.** Deactivating by
"just setting it back to READY" loses the fact that a model was live and was
withdrawn, which is exactly the fact anybody investigating later needs. It is
also the only action that is *always* available on an ACTIVE version: an
operator taking a bad model out must never be told the state machine forbids
it.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping

from academic_ai.domain.enums import AIModelStatus

__all__ = [
    "ACTION_TRANSITIONS",
    "TERMINAL_STATUSES",
    "LifecycleError",
    "ModelAction",
    "ModelVersionState",
    "apply_action",
    "is_serving_status",
    "permitted_actions",
]


class LifecycleError(RuntimeError):
    """A transition that the registry refuses. Never a silent no-op."""


class ModelAction(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    REGISTER = "REGISTER"
    UPLOAD = "UPLOAD"
    TEST = "TEST"
    BENCHMARK = "BENCHMARK"
    COMPARE = "COMPARE"
    ACTIVATE = "ACTIVATE"
    ROLLBACK = "ROLLBACK"
    DISABLE = "DISABLE"


_E = AIModelStatus.EXPERIMENTAL
_T = AIModelStatus.TESTING
_R = AIModelStatus.READY
_A = AIModelStatus.ACTIVE
_D = AIModelStatus.DEPRECATED
_X = AIModelStatus.DISABLED

#: action -> {status it may be applied in: status it produces}.
#:
#: Read the ACTIVATE row on its own: exactly one source state, and
#: `apply_action` additionally demands an actor for it. Read the DISABLE row
#: too — it is available from everywhere *including* ACTIVE, because a switch
#: that only works when nothing is wrong is not a switch.
ACTION_TRANSITIONS: Mapping[ModelAction, Mapping[AIModelStatus, AIModelStatus]] = {
    # REGISTER creates the row; it is listed for completeness and is the only
    # action whose "from" state is the absence of one.
    ModelAction.REGISTER: {},
    # Uploading an artifact does not change the state. A version with an
    # artifact is still EXPERIMENTAL — having a file is not evidence.
    ModelAction.UPLOAD: {_E: _E, _T: _T},
    ModelAction.TEST: {_E: _T, _T: _T},
    # BENCHMARK is what earns READY: it is the first action that produces a
    # number comparable with the heuristic.
    ModelAction.BENCHMARK: {_T: _R, _R: _R},
    # COMPARE reads two versions and changes neither.
    ModelAction.COMPARE: {_E: _E, _T: _T, _R: _R, _A: _A, _D: _D, _X: _X},
    ModelAction.ACTIVATE: {_R: _A},
    # Withdrawing a live model. DEPRECATED rather than READY: the version was
    # once serving, and a later reader needs to know that.
    ModelAction.ROLLBACK: {_A: _D},
    # Everything except DISABLED itself: the state is terminal, and an
    # idempotent second DISABLE would swallow the caller bug that made it.
    ModelAction.DISABLE: {_E: _X, _T: _X, _R: _X, _A: _X, _D: _X},
}

#: Once here, a version never serves again without being re-registered.
TERMINAL_STATUSES = frozenset({AIModelStatus.DISABLED})


def is_serving_status(status: AIModelStatus) -> bool:
    """Whether a version in this status may be asked for a prediction."""
    return status is AIModelStatus.ACTIVE


@dataclass(frozen=True, slots=True)
class ModelVersionState:
    """The mutable part of a registered version.

    A value object rather than the ORM row: `apply_action` has to be callable
    from a worker with no database, and returning a new state rather than
    mutating one keeps the transition auditable — the caller holds both the
    before and the after, which is what the audit log stores.
    """

    model_code: str
    version: str
    status: AIModelStatus = AIModelStatus.EXPERIMENTAL
    has_artifact: bool = False
    #: Set by TEST. A version cannot be benchmarked before it has been run.
    tested_at: dt.datetime | None = None
    #: Set by BENCHMARK.
    benchmarked_at: dt.datetime | None = None
    activated_by_user_id: str | None = None
    activated_at: dt.datetime | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)

    @property
    def is_serving(self) -> bool:
        return is_serving_status(self.status)


def permitted_actions(state: ModelVersionState) -> frozenset[ModelAction]:
    """Every action legal from here, ignoring the extra preconditions.

    Reported so a UI can grey out a button rather than let somebody press it
    and read a refusal. `apply_action` remains the authority: this function
    answers about the state table only, and ACTIVATE additionally needs an
    actor and a tested, benchmarked, artifact-bearing version.
    """
    return frozenset(
        action
        for action, table in ACTION_TRANSITIONS.items()
        if state.status in table
    )


def apply_action(
    state: ModelVersionState,
    action: ModelAction,
    *,
    actor_user_id: str | None = None,
    now: dt.datetime | None = None,
    metrics: Mapping[str, Any] | None = None,
) -> ModelVersionState:
    """Apply one action, or refuse and say why.

    Refusals carry the current status and the action, because "invalid state
    transition" in a log at 02:00 tells the reader nothing they did not
    already suspect.
    """
    if action is ModelAction.REGISTER:
        raise LifecycleError(
            "REGISTER creates a version; it is not applied to an existing one"
        )

    table = ACTION_TRANSITIONS[action]
    if state.status not in table:
        legal = ", ".join(sorted(a.value for a in permitted_actions(state))) or "none"
        raise LifecycleError(
            f"{state.model_code} {state.version}: cannot {action.value} from "
            f"{state.status.value}. Legal here: {legal}."
        )

    now = now or dt.datetime.now(dt.timezone.utc)
    updated = replace(state, status=table[state.status])

    if action is ModelAction.UPLOAD:
        return replace(updated, has_artifact=True)

    if action is ModelAction.TEST:
        if not state.has_artifact:
            raise LifecycleError(
                f"{state.model_code} {state.version}: TEST needs an uploaded "
                "artifact. Testing a version with no artifact tests the "
                "heuristic and records the result against the model."
            )
        return replace(updated, tested_at=now)

    if action is ModelAction.BENCHMARK:
        if state.tested_at is None:
            raise LifecycleError(
                f"{state.model_code} {state.version}: BENCHMARK needs TEST "
                "first. A benchmark of a version that has never run produces "
                "a number, which is worse than producing nothing."
            )
        return replace(
            updated,
            benchmarked_at=now,
            metrics=dict(metrics) if metrics is not None else dict(state.metrics),
        )

    if action is ModelAction.COMPARE:
        return state  # reads two versions, changes neither

    if action is ModelAction.ACTIVATE:
        if not actor_user_id:
            raise LifecycleError(
                f"{state.model_code} {state.version}: ACTIVATE requires the "
                "user id of the person activating it. No model activates "
                "automatically (CLAUDE.md — Human authority), and an "
                "unattributed activation is an automatic one with a blank "
                "where the name goes."
            )
        if state.benchmarked_at is None:
            raise LifecycleError(
                f"{state.model_code} {state.version}: ACTIVATE requires a "
                "BENCHMARK. READY without one means nobody has compared this "
                "model to the heuristic it is about to replace."
            )
        return replace(updated, activated_by_user_id=actor_user_id, activated_at=now)

    if action is ModelAction.ROLLBACK:
        # The activation record is kept. Who put it live is precisely what an
        # investigation into why it was withdrawn needs.
        return updated

    if action is ModelAction.DISABLE:
        return updated

    raise LifecycleError(f"unhandled action {action}")  # pragma: no cover
