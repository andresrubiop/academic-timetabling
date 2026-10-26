"""Creating and managing the people who use the platform.

Until now the only account that could exist was the one `bootstrap` made. A
second person needed somebody to run a command inside the container, which
made the row-level authorisation of SESSIONS 23-26 unreachable in practice:
there was nobody to assign a scope *to*.

Three rules make this more than a table.

**Nobody may create or promote above their own role.** An `ADMIN` creating a
`SUPER_ADMIN` is privilege escalation with extra steps — and the same is true
of promoting an existing account, so both paths are checked. Today only
institution-wide roles hold `MANAGE_USERS`, so the rule mostly refuses
`ADMIN` reaching for `SUPER_ADMIN`; it becomes load-bearing the moment a
narrower role is given the permission.

**Nobody may deactivate themselves.** An administrator who does is locked out
of the platform, and the fix is a shell on the container. It is not a
hypothetical: the button is next to the one for everybody else.

**A created account gets a temporary password it must change.** `must_change`
existed from SESSION 8 and was enforced nowhere until SESSION 33; it means
something now, so an administrator never needs to know somebody's real
password.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import select

from academic_ai.domain.enums import AuditAction, UserRole
from academic_ai.security.rbac import Permission, Principal, authorise

__all__ = [
    "UserAccount",
    "UserManagementError",
    "create_user",
    "list_users",
    "set_active",
    "set_role",
]

log = logging.getLogger("academic_ai.users")

#: Highest first. "Above your own role" is decided by this order and nothing
#: else, so adding a role means placing it here deliberately rather than
#: inheriting whatever the enum's declaration order happens to be.
_SENIORITY: tuple[UserRole, ...] = (
    UserRole.SUPER_ADMIN,
    UserRole.ADMIN,
    UserRole.DEAN,
    UserRole.DIRECTOR,
    UserRole.COORDINATOR,
    UserRole.TEACHER,
    UserRole.STUDENT,
)


class UserManagementError(RuntimeError):
    """Refused, with a reason an administrator can act on."""


@dataclass(frozen=True, slots=True)
class UserAccount:
    user_id: str
    email: str
    full_name: str
    role: str
    is_active: bool
    must_change_password: bool
    #: Only ever set on the response to *creating* an account, and never
    #: stored or logged. An administrator has one chance to pass it on.
    temporary_password: str | None = None


def _rank(role: UserRole) -> int:
    try:
        return _SENIORITY.index(role)
    except ValueError:  # pragma: no cover - a role not placed above
        # An unplaced role is treated as the most junior, so forgetting to
        # rank a new one cannot grant seniority by accident.
        return len(_SENIORITY)


def _refuse_reaching_above(actor: Principal, target_role: UserRole) -> None:
    if _rank(target_role) < _rank(actor.role):
        raise UserManagementError(
            f"you cannot create or promote a {target_role.value}: it outranks "
            f"your own {actor.role.value}"
        )


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError as exc:
        raise UserManagementError(f"{value!r} is not an id") from exc


def _audit(session, actor: Principal, action: AuditAction, user_id, **fields) -> None:
    from academic_ai.infrastructure.db.models import AuditLog

    session.add(
        AuditLog(
            actor_user_id=_as_uuid(actor.user_id),
            actor_description=f"{actor.role.value} via API",
            action=action,
            entity_type="app_user",
            entity_id=user_id,
            after=fields or None,
        )
    )


def _account(
    user,
    credential=None,
    *,
    temporary: str | None = None,
    must_change: bool | None = None,
) -> UserAccount:
    return UserAccount(
        user_id=str(user.id),
        email=user.email,
        full_name=user.full_name,
        role=user.primary_role.value,
        is_active=user.is_active,
        must_change_password=(
            must_change
            if must_change is not None
            else bool(credential and credential.must_change)
        ),
        temporary_password=temporary,
    )


def list_users(session, principal: Principal) -> Sequence[UserAccount]:
    """Every account. Not narrowed to the caller's jurisdiction: an
    administrator asking who exists needs the whole answer, and a partial one
    would make "is this person already registered" depend on who asked."""
    from academic_ai.infrastructure.db.models import User, UserCredential

    authorise(principal, Permission.MANAGE_USERS)
    credentials = {
        c.app_user_id: c for c in session.scalars(select(UserCredential))
    }
    return [
        _account(user, credentials.get(user.id))
        for user in session.scalars(select(User).order_by(User.email))
    ]


def create_user(
    session,
    principal: Principal,
    *,
    email: str,
    full_name: str,
    role: UserRole,
) -> UserAccount:
    """Create an account with a temporary password, returned once."""
    from academic_ai.infrastructure.db.models import User, UserCredential
    from academic_ai.security.passwords import hash_password

    authorise(principal, Permission.MANAGE_USERS)
    _refuse_reaching_above(principal, role)

    email = email.strip().lower()
    if not email or "@" not in email:
        raise UserManagementError(f"{email!r} is not an email address")
    if session.scalars(select(User).where(User.email == email)).first():
        raise UserManagementError(f"{email} already exists")

    user = User(
        email=email,
        full_name=full_name.strip() or email,
        primary_role=role,
        is_active=True,
    )
    session.add(user)
    session.flush()

    # Long enough that it is not worth guessing, and generated here so no
    # administrator ever types somebody else's password. `must_change` makes
    # it good for exactly one sign-in.
    temporary = secrets.token_urlsafe(18)
    session.add(
        UserCredential(
            app_user_id=user.id,
            password_hash=hash_password(temporary),
            must_change=True,
        )
    )
    session.flush()

    _audit(session, principal, AuditAction.CREATE, user.id, role=role.value)
    log.info(
        "user created",
        extra={"actor": principal.user_id, "role": role.value},
    )
    return _account(user, temporary=temporary, must_change=True)


def set_role(
    session, principal: Principal, user_id: uuid.UUID | str, role: UserRole
) -> UserAccount:
    """Promote or demote. Cannot reach above the caller's own role."""
    from academic_ai.infrastructure.db.models import User

    authorise(principal, Permission.MANAGE_USERS)
    _refuse_reaching_above(principal, role)

    user = session.get(User, _as_uuid(user_id))
    if user is None:
        raise UserManagementError(f"no user {user_id}")
    # Demoting somebody senior to you is the same escalation seen from the
    # other side: it removes an authority that could have stopped you.
    _refuse_reaching_above(principal, user.primary_role)

    before = user.primary_role
    user.primary_role = role
    session.flush()
    _audit(
        session,
        principal,
        AuditAction.UPDATE,
        user.id,
        role_from=before.value,
        role_to=role.value,
    )
    return _account(user)


def set_active(
    session, principal: Principal, user_id: uuid.UUID | str, is_active: bool
) -> UserAccount:
    """Deactivate or restore an account.

    Deactivating rather than deleting, always: an account that published a
    timetable or approved a change cannot be removed without erasing who did
    those things, and the schema refuses it anyway.
    """
    from academic_ai.infrastructure.db.models import User

    authorise(principal, Permission.MANAGE_USERS)

    user = session.get(User, _as_uuid(user_id))
    if user is None:
        raise UserManagementError(f"no user {user_id}")
    if not is_active and str(user.id) == principal.user_id:
        raise UserManagementError(
            "you cannot deactivate your own account; the way back in is a "
            "shell on the container"
        )
    _refuse_reaching_above(principal, user.primary_role)

    user.is_active = is_active
    session.flush()
    _audit(
        session,
        principal,
        AuditAction.ACTIVATE if is_active else AuditAction.DEACTIVATE,
        user.id,
    )
    return _account(user)
