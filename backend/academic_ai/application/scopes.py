"""Granting and revoking authority over a faculty or a programme.

The write side of `user_scope_assignment`. SESSION 23 built the table and the
model that reads it; nothing but tests wrote to it, so scoped roles could only
ever hold nothing and enforcement could not be switched on.

Two rules make this more than a CRUD table.

**Nobody may grant what they do not hold.** An administrator with authority
over one faculty may assign a coordinator inside it and nowhere else. Today
only institution-wide roles carry `MANAGE_USERS`, so the check refuses nothing
— which is exactly when to write it. The day `MANAGE_USERS` is given to a
dean, delegation becomes a privilege-escalation path, and nobody will remember
to add the check then.

**Every grant and revoke is audited.** `audit_log` has existed since SESSION 1,
is append-only by trigger, and until now nothing wrote to it. Handing somebody
authority over a faculty is the canonical thing an audit trail is for: the row
itself records who created it, but a revoke removes the row and would take
that fact with it.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.domain.enums import AuditAction, UserRole
from academic_ai.security.jurisdiction import Breadth, breadth_for
from academic_ai.security.rbac import Permission, Principal, authorise

__all__ = [
    "ScopeAssignmentError",
    "ScopeGrant",
    "grant_scope",
    "list_scopes",
    "revoke_scope",
]

log = logging.getLogger("academic_ai.scopes")


class ScopeAssignmentError(RuntimeError):
    """Refused, with a reason a person can act on."""


@dataclass(frozen=True, slots=True)
class ScopeGrant:
    """One assignment, as the API reports it."""

    assignment_id: str
    user_id: str
    faculty_id: str | None
    program_id: str | None
    note: str


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError as exc:
        raise ScopeAssignmentError(f"{value!r} is not an id") from exc


def _audit(
    session: Session,
    principal: Principal,
    action: AuditAction,
    assignment_id: uuid.UUID,
    *,
    before: dict | None = None,
    after: dict | None = None,
) -> None:
    from academic_ai.infrastructure.db.models import AuditLog

    session.add(
        AuditLog(
            actor_user_id=_as_uuid(principal.user_id),
            actor_description=f"{principal.role.value} via API",
            action=action,
            entity_type="user_scope_assignment",
            entity_id=assignment_id,
            before=before,
            after=after,
        )
    )


def list_scopes(
    session: Session, principal: Principal, user_id: uuid.UUID | str
) -> Sequence[ScopeGrant]:
    """Every scope this user holds.

    Readable by anyone who may manage users. Not narrowed to the caller's own
    jurisdiction: an administrator asking "what does this person hold" needs
    the whole answer, and a partial one would make an audit of somebody's
    authority depend on who ran it.
    """
    from academic_ai.infrastructure.db.models import UserScopeAssignment

    authorise(principal, Permission.MANAGE_USERS)
    rows = session.scalars(
        select(UserScopeAssignment)
        .where(UserScopeAssignment.app_user_id == _as_uuid(user_id))
        .order_by(UserScopeAssignment.created_at)
    ).all()
    return [
        ScopeGrant(
            assignment_id=str(row.id),
            user_id=str(row.app_user_id),
            faculty_id=str(row.faculty_id) if row.faculty_id else None,
            program_id=str(row.program_id) if row.program_id else None,
            note=row.note or "",
        )
        for row in rows
    ]


def grant_scope(
    session: Session,
    principal: Principal,
    user_id: uuid.UUID | str,
    *,
    faculty_id: uuid.UUID | str | None = None,
    program_id: uuid.UUID | str | None = None,
    note: str = "",
) -> ScopeGrant:
    """Give a user authority over one faculty or one programme."""
    from academic_ai.infrastructure.db.models import User, UserScopeAssignment

    authorise(principal, Permission.MANAGE_USERS)

    if (faculty_id is None) == (program_id is None):
        # The database enforces this too. Refusing here means the caller gets
        # a sentence rather than a constraint name.
        raise ScopeAssignmentError(
            "a scope assignment names exactly one faculty or one programme"
        )

    target = session.get(User, _as_uuid(user_id))
    if target is None:
        raise ScopeAssignmentError(f"no user {user_id}")

    if breadth_for(target.primary_role) is Breadth.OWN:
        # A teacher or a student holds authority over nobody. An assignment
        # would be inert, and an inert row in this table is worse than none:
        # an audit of "who is responsible for this programme" would list them.
        raise ScopeAssignmentError(
            f"{target.primary_role.value} holds no authority over others, so a "
            "scope assignment would mean nothing"
        )

    _refuse_granting_beyond_own_authority(
        principal, faculty_id=faculty_id, program_id=program_id
    )

    row = UserScopeAssignment(
        app_user_id=target.id,
        faculty_id=_as_uuid(faculty_id) if faculty_id else None,
        program_id=_as_uuid(program_id) if program_id else None,
        note=note.strip() or None,
        created_by_user_id=_as_uuid(principal.user_id),
    )
    session.add(row)
    session.flush()

    _audit(
        session,
        principal,
        AuditAction.CREATE,
        row.id,
        after={
            "app_user_id": str(row.app_user_id),
            "faculty_id": str(row.faculty_id) if row.faculty_id else None,
            "program_id": str(row.program_id) if row.program_id else None,
        },
    )
    log.info(
        "scope granted",
        extra={
            "actor": principal.user_id,
            "target_user": str(target.id),
            "scope_kind": "faculty" if faculty_id else "program",
        },
    )
    return ScopeGrant(
        assignment_id=str(row.id),
        user_id=str(row.app_user_id),
        faculty_id=str(row.faculty_id) if row.faculty_id else None,
        program_id=str(row.program_id) if row.program_id else None,
        note=row.note or "",
    )


def revoke_scope(
    session: Session, principal: Principal, assignment_id: uuid.UUID | str
) -> None:
    """Take an assignment away.

    A hard delete, with an audit row. The assignment carries no lifecycle of
    its own — "retired authority" is not a state anybody acts on — but *who
    used to be responsible* is exactly the question an investigation asks, and
    deleting the row would take it with it.
    """
    from academic_ai.infrastructure.db.models import UserScopeAssignment

    authorise(principal, Permission.MANAGE_USERS)

    row = session.get(UserScopeAssignment, _as_uuid(assignment_id))
    if row is None:
        raise ScopeAssignmentError(f"no scope assignment {assignment_id}")

    _refuse_granting_beyond_own_authority(
        principal,
        faculty_id=str(row.faculty_id) if row.faculty_id else None,
        program_id=str(row.program_id) if row.program_id else None,
    )

    before = {
        "app_user_id": str(row.app_user_id),
        "faculty_id": str(row.faculty_id) if row.faculty_id else None,
        "program_id": str(row.program_id) if row.program_id else None,
        "note": row.note,
    }
    assignment_uuid = row.id
    session.delete(row)
    session.flush()
    _audit(session, principal, AuditAction.REVOKE, assignment_uuid, before=before)
    log.info(
        "scope revoked",
        extra={"actor": principal.user_id, "target_user": before["app_user_id"]},
    )


def _refuse_granting_beyond_own_authority(
    principal: Principal,
    *,
    faculty_id: uuid.UUID | str | None,
    program_id: uuid.UUID | str | None,
) -> None:
    """You cannot hand out authority you do not hold.

    Refuses nothing today — `MANAGE_USERS` belongs only to institution-wide
    roles — and that is the argument for writing it now rather than later.
    Delegation is how a scoped administrator escalates: grant an ally
    authority over a faculty you do not hold, and act through them.
    """
    jurisdiction = principal.jurisdiction
    if jurisdiction.is_institution_wide:
        return

    if faculty_id is not None and not jurisdiction.covers_faculty(str(faculty_id)):
        raise ScopeAssignmentError(
            "you cannot assign authority over a faculty you do not hold"
        )
    if program_id is not None and not jurisdiction.covers_program(str(program_id)):
        raise ScopeAssignmentError(
            "you cannot assign authority over a programme you do not hold"
        )
