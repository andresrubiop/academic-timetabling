"""User accounts.

Not the generic CRUD factory: creating an account mints a credential, and the
rules that matter here — nobody creates above their own role, nobody
deactivates themselves — are not things a `CrudConfig` can express. A table
where those live in a config object is a table where the next maintainer
removes them by deleting a line.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from academic_ai.api.deps import get_session, require
from academic_ai.api.schemas import (
    CreateUserRequest,
    SetActiveRequest,
    SetRoleRequest,
    UserAccountModel,
    UserListResponse,
)
from academic_ai.application.users import (
    UserManagementError,
    create_user,
    list_users,
    set_active,
    set_role,
)
from academic_ai.domain.enums import UserRole
from academic_ai.security.rbac import Permission, Principal

router = APIRouter(prefix="/users", tags=["users"])
log = logging.getLogger("academic_ai.api.users")


def _model(account) -> UserAccountModel:
    return UserAccountModel(
        user_id=account.user_id,
        email=account.email,
        full_name=account.full_name,
        role=account.role,
        is_active=account.is_active,
        must_change_password=account.must_change_password,
        temporary_password=account.temporary_password,
    )


def _role(raw: str) -> UserRole:
    try:
        return UserRole[raw]
    except KeyError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"{raw!r} is not a role; expected one of "
            + ", ".join(r.value for r in UserRole),
        ) from None


@router.get("", response_model=UserListResponse)
def get_users(
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.MANAGE_USERS)),
) -> UserListResponse:
    return UserListResponse(
        users=[_model(a) for a in list_users(session, principal)]
    )


@router.post("", response_model=UserAccountModel, status_code=status.HTTP_201_CREATED)
def post_user(
    payload: CreateUserRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.MANAGE_USERS)),
) -> UserAccountModel:
    """Create an account. The temporary password comes back **once**."""
    try:
        account = create_user(
            session,
            principal,
            email=payload.email,
            full_name=payload.full_name,
            role=_role(payload.role),
        )
    except UserManagementError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    session.commit()
    return _model(account)


@router.patch("/{user_id}/role", response_model=UserAccountModel)
def patch_role(
    user_id: str,
    payload: SetRoleRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.MANAGE_USERS)),
) -> UserAccountModel:
    try:
        account = set_role(session, principal, user_id, _role(payload.role))
    except UserManagementError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    session.commit()
    return _model(account)


@router.patch("/{user_id}/active", response_model=UserAccountModel)
def patch_active(
    user_id: str,
    payload: SetActiveRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require(Permission.MANAGE_USERS)),
) -> UserAccountModel:
    """Deactivate or restore. Never a delete: an account that published a
    timetable cannot be removed without erasing who did it."""
    try:
        account = set_active(session, principal, user_id, payload.is_active)
    except UserManagementError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    session.commit()
    return _model(account)
