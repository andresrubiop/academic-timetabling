"""Dependencies: settings, database session, queue, and who is calling.

`require(permission)` is the only authorisation entry point in the API. It
calls `authorise()` from the security layer rather than reimplementing the
check, so the five decisions CLAUDE.md reserves to humans are enforced here by
the same table a test can read.
"""

from __future__ import annotations

import functools
import os
from typing import Iterator

import redis
from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from academic_ai.application.auth import AuthError, AuthService
from academic_ai.application.optimization import OptimizationService
from academic_ai.infrastructure.db.session import session_scope
from academic_ai.infrastructure.queue import RedisJobQueue
from academic_ai.security.rbac import AuthorizationError, Permission, Principal
from academic_ai.security.settings import SecuritySettings, load_security_settings

__all__ = [
    "get_auth_service",
    "get_optimization_service",
    "get_principal",
    "get_queue",
    "get_session",
    "get_settings",
    "require",
]


@functools.lru_cache(maxsize=1)
def get_settings() -> SecuritySettings:
    """Loaded once. Validated at import of the app, so a misconfigured
    deployment fails at startup rather than on the first request."""
    return load_security_settings()


@functools.lru_cache(maxsize=1)
def _redis_client() -> redis.Redis:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        raise RuntimeError("REDIS_URL is not set; the job queue cannot be reached")
    return redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=5)


def get_queue() -> RedisJobQueue:
    return RedisJobQueue(_redis_client())


def get_session() -> Iterator[Session]:
    yield from session_scope()


def get_auth_service(
    session: Session = Depends(get_session),
    settings: SecuritySettings = Depends(get_settings),
) -> AuthService:
    return AuthService(session, settings)


def get_optimization_service(
    session: Session = Depends(get_session),
    queue: RedisJobQueue = Depends(get_queue),
) -> OptimizationService:
    return OptimizationService(session, queue)


def get_principal(
    authorization: str | None = Header(default=None),
    auth: AuthService = Depends(get_auth_service),
) -> Principal:
    """Resolve the caller, or 401.

    The role comes from the *database*, not from the token: a token minted
    before a demotion would otherwise carry its old privileges until it
    expired.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.split(" ", 1)[1].strip()
    try:
        return auth.principal_from_access_token(token)
    except AuthError:
        # One message for every cause. "Expired" versus "invalid signature"
        # tells an attacker which half of a forgery worked.
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


def require(permission: Permission):
    """A dependency that enforces one permission.

    Returns the principal, so a route that needs both authorisation and the
    caller's identity gets them from one dependency rather than two that could
    disagree.
    """

    def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        from academic_ai.security.rbac import authorise

        if principal.must_change_password:
            # Every permission is refused until the temporary password an
            # administrator issued is replaced. The flag existed from
            # SESSION 8, was reported at login, and was enforced nowhere — so
            # a temporary password worked indefinitely, which is not
            # temporary.
            #
            # `/auth/me`, `/auth/change-password` and `/auth/logout` depend on
            # `get_principal` rather than on this, so they stay reachable:
            # refusing the endpoint that clears the flag would lock the
            # account out permanently.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    "your password must be changed before you can use the "
                    "platform: POST /api/v1/auth/change-password"
                ),
            )

        try:
            authorise(principal, permission)
        except AuthorizationError as exc:
            # 403, not 404: the caller is authenticated and the resource kind
            # exists. Hiding that would make every permission error look like
            # a broken URL.
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)
            ) from exc
        return principal

    return _dependency
