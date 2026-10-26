"""Access and refresh tokens.

Short-lived signed access tokens, long-lived *revocable* refresh tokens. The
split exists so that logging out means something: a stateless access token
cannot be withdrawn, so it is given a life measured in minutes, and the thing
that lasts a week is a handle the server can delete.

Decisions worth stating
=======================

**HS256, not RS256.** There is one issuer and one verifier, both inside this
project's Docker network. Asymmetric signing buys key separation this
deployment has nobody to separate from, at the cost of key management on a
host where `sudo` needs a password.

**The token type is inside the signature.** ``typ`` is a claim, not a
convention, and `decode_token` refuses a refresh token presented as an access
token. Without that, the week-long credential is silently accepted wherever
the thirty-minute one is, and the whole split is decorative.

**Every refresh token carries a `jti`.** It is the handle a `user_session`
row is keyed on, which is what makes revocation possible at all. Access
tokens carry one too, so a specific leaked token can be denied without
invalidating the session it came from.

**Expiry is verified, never trusted.** `jwt.decode` checks `exp` itself; this
module does not re-implement that, and does not offer a way to skip it.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping

import jwt

__all__ = [
    "ALGORITHM",
    "TokenError",
    "TokenType",
    "TokenClaims",
    "decode_token",
    "issue_access_token",
    "issue_refresh_token",
]

#: One issuer, one verifier, both inside the project's Docker network.
ALGORITHM = "HS256"

#: Rejecting "none" is the whole reason this is a constant and not a
#: parameter. `jwt.decode(..., algorithms=[...])` with an attacker-influenced
#: list is the classic JWT forgery.
_ACCEPTED_ALGORITHMS = (ALGORITHM,)


class TokenType(str, Enum):
    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value

    ACCESS = "access"
    REFRESH = "refresh"


class TokenError(ValueError):
    """Invalid, expired, or the wrong type. Never says which secret failed."""


@dataclass(frozen=True, slots=True)
class TokenClaims:
    """What a verified token asserts."""

    subject: str
    token_type: TokenType
    jti: str
    issued_at: dt.datetime
    expires_at: dt.datetime
    role: str | None = None
    extra: Mapping[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.extra is None:
            object.__setattr__(self, "extra", {})


def _issue(
    *,
    subject: str,
    secret: str,
    token_type: TokenType,
    lifetime: dt.timedelta,
    role: str | None,
    issuer: str,
    audience: str,
    now: dt.datetime | None,
    extra: Mapping[str, Any] | None,
) -> tuple[str, str]:
    if not secret:
        raise TokenError("refusing to sign a token with an empty secret")
    now = now or dt.datetime.now(dt.timezone.utc)
    jti = str(uuid.uuid4())
    payload: dict[str, Any] = {
        "sub": subject,
        "typ": token_type.value,
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "iss": issuer,
        "aud": audience,
    }
    if role is not None:
        payload["role"] = role
    for key, value in (extra or {}).items():
        # Reserved claims are the ones verification depends on. Letting a
        # caller pass `typ` or `exp` through `extra` would let a convenience
        # kwarg mint an access token that never expires.
        if key in payload:
            raise TokenError(f"{key!r} is a reserved claim and cannot be overridden")
        payload[key] = value
    return jwt.encode(payload, secret, algorithm=ALGORITHM), jti


def issue_access_token(
    *,
    subject: str,
    secret: str,
    role: str | None = None,
    lifetime_minutes: int = 30,
    issuer: str = "academic-ai",
    audience: str = "academic-ai",
    now: dt.datetime | None = None,
    extra: Mapping[str, Any] | None = None,
) -> tuple[str, str]:
    """Returns ``(token, jti)``. Minutes, because it cannot be revoked."""
    return _issue(
        subject=subject,
        secret=secret,
        token_type=TokenType.ACCESS,
        lifetime=dt.timedelta(minutes=lifetime_minutes),
        role=role,
        issuer=issuer,
        audience=audience,
        now=now,
        extra=extra,
    )


def issue_refresh_token(
    *,
    subject: str,
    secret: str,
    lifetime_days: int = 7,
    issuer: str = "academic-ai",
    audience: str = "academic-ai",
    now: dt.datetime | None = None,
) -> tuple[str, str]:
    """Returns ``(token, jti)``.

    Carries no role. A refresh token that embedded one would keep handing out
    access tokens with yesterday's privileges after somebody was demoted, and
    a demotion that does not take effect until next week is not a demotion.
    """
    return _issue(
        subject=subject,
        secret=secret,
        token_type=TokenType.REFRESH,
        lifetime=dt.timedelta(days=lifetime_days),
        role=None,
        issuer=issuer,
        audience=audience,
        now=now,
        extra=None,
    )


def decode_token(
    token: str,
    *,
    secret: str,
    expected_type: TokenType,
    issuer: str = "academic-ai",
    audience: str = "academic-ai",
) -> TokenClaims:
    """Verify signature, expiry, issuer, audience **and type**.

    ``expected_type`` is required rather than defaulted: a caller who does not
    say which kind of token they are expecting has not decided, and the
    default would inevitably become ACCESS — the permissive direction.
    """
    if not secret:
        raise TokenError("refusing to verify a token with an empty secret")
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=list(_ACCEPTED_ALGORITHMS),
            issuer=issuer,
            audience=audience,
            options={"require": ["exp", "iat", "sub", "jti", "typ", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token has expired") from exc
    except jwt.InvalidTokenError as exc:
        # One message for every failure. Distinguishing "bad signature" from
        # "wrong issuer" tells an attacker which half of the forgery worked.
        raise TokenError("token is not valid") from exc

    actual = payload.get("typ")
    if actual != expected_type.value:
        raise TokenError(
            f"expected a {expected_type.value} token, got {actual!r}"
        )

    reserved = {"sub", "typ", "jti", "iat", "exp", "iss", "aud", "role"}
    return TokenClaims(
        subject=payload["sub"],
        token_type=expected_type,
        jti=payload["jti"],
        issued_at=dt.datetime.fromtimestamp(payload["iat"], dt.timezone.utc),
        expires_at=dt.datetime.fromtimestamp(payload["exp"], dt.timezone.utc),
        role=payload.get("role"),
        extra={k: v for k, v in payload.items() if k not in reserved},
    )
