"""Login, refresh, logout, and "who am I".

Every failure returns the same 401 with the same body. Distinguishing "no such
account" from "wrong password" turns this router into an account enumerator,
and "your account is locked" tells an attacker their guessing is working.

`Retry-After` on a lockout is the one exception, and it is deliberate: it is
sent only after a *correct* identification of an already-locked account, tells
the legitimate user when to come back, and gives an attacker nothing they
could not learn by waiting.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from academic_ai.api.deps import get_auth_service, get_principal
from academic_ai.api.schemas import (
    ChangePasswordRequest,
    LoginRequest,
    MeResponse,
    RefreshRequest,
    TokenResponse,
)
from academic_ai.application.auth import AuthError, AuthService, LockedOut
from academic_ai.security.rbac import Principal

router = APIRouter(prefix="/auth", tags=["auth"])

_UNAUTHENTICATED = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="invalid credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


@router.post("/login", response_model=TokenResponse)
def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    auth: AuthService = Depends(get_auth_service),
) -> TokenResponse:
    try:
        result = auth.login(
            payload.email,
            payload.password,
            # X-Forwarded-For is set by our own nginx, which is the only
            # thing that can reach this process. Recorded for audit only --
            # never used for authorisation, since a session pinned to an IP
            # breaks every user on a mobile network and stops no attacker who
            # already has the token.
            client_ip=request.headers.get("x-forwarded-for", request.client.host if request.client else None),
            user_agent=request.headers.get("user-agent"),
        )
    except LockedOut as exc:
        response.headers["Retry-After"] = str(exc.retry_after_seconds)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="too many attempts; try again later",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except AuthError:
        raise _UNAUTHENTICATED from None

    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        must_change_password=result.must_change_password,
    )


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    payload: RefreshRequest, auth: AuthService = Depends(get_auth_service)
) -> TokenResponse:
    """Exchange a refresh token for a new pair. The old one stops working.

    Rotation is not optional: a refresh token that stays valid after use can
    be replayed for its whole lifetime by whoever stole it.
    """
    try:
        result = auth.refresh(payload.refresh_token)
    except AuthError:
        raise _UNAUTHENTICATED from None
    return TokenResponse(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        expires_in=result.expires_in,
        must_change_password=result.must_change_password,
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    payload: RefreshRequest, auth: AuthService = Depends(get_auth_service)
) -> Response:
    """Revoke one session. Always 204, even for a token that was never valid.

    A client with a token it cannot use is exactly the client trying to
    discard it; failing here would leave the token in their storage.
    """
    auth.logout(payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    payload: ChangePasswordRequest,
    auth: AuthService = Depends(get_auth_service),
    principal: Principal = Depends(get_principal),
) -> None:
    """Change your own password.

    Depends on `get_principal` rather than `require()`, so it stays reachable
    while a forced change is pending — refusing the endpoint that clears the
    flag would lock the account out permanently.
    """
    from academic_ai.security.passwords import MIN_PASSWORD_LENGTH

    if len(payload.new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"the password must be at least {MIN_PASSWORD_LENGTH} characters",
        )
    try:
        auth.change_password(
            principal.user_id, payload.current_password, payload.new_password
        )
    except AuthError as exc:
        # 400 rather than 401: the caller is authenticated. A 401 would send
        # a browser to the sign-in screen for a mistyped current password.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc) or "invalid credentials"
        ) from exc


@router.get("/me", response_model=MeResponse)
def me(principal: Principal = Depends(get_principal)) -> MeResponse:
    return MeResponse(
        user_id=principal.user_id,
        role=principal.role.value,
        permissions=sorted(p.value for p in principal.permissions),
    )
