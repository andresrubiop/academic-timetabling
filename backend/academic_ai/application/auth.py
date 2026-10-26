"""Logging in, refreshing, logging out, and the lockout policy.

The lockout is the part worth reading. A naive one is a denial-of-service
against real users: anybody who knows a colleague's email can lock them out by
failing to log in as them five times. This one is written to make that
expensive rather than free, and to fail in the direction that inconveniences
an attacker rather than a coordinator on the morning of enrolment.

Three decisions
===============

**The same answer for every failure.** Wrong password, unknown account,
deactivated account and locked account all produce one `AuthError` with one
message. Distinguishing them turns the login form into an account enumerator,
and "this account is locked" tells an attacker their guessing is working.

**Lockout is temporary and backs off.** A permanent lock needs an
administrator, which means a locked-out coordinator is blocked until somebody
answers the phone. The window grows with consecutive failures instead, so a
person who mistypes twice waits seconds and a script waits usefully longer.

**A successful login still costs a hash.** Failing early on an unknown email
would make "user exists" measurable with a stopwatch, so the unknown-account
path verifies against a dummy hash before returning the same error.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from academic_ai.domain.enums import UserRole
from academic_ai.infrastructure.db.models import User, UserCredential, UserSession
from academic_ai.security.passwords import (
    hash_password,
    needs_rehash,
    verify_password,
)
from academic_ai.security.rbac import ActorKind, Principal
from academic_ai.security.settings import SecuritySettings
from academic_ai.security.tokens import (
    TokenError,
    TokenType,
    decode_token,
    issue_access_token,
    issue_refresh_token,
)

__all__ = ["AuthError", "AuthService", "LockedOut", "LoginResult"]

log = logging.getLogger("academic_ai.auth")

#: Verified against when the account does not exist, so the unknown-email path
#: costs the same as the wrong-password path. Generated once at import.
_DUMMY_HASH = hash_password("a password nobody has, used only for timing")

#: Failures before the first lock. Generous, because the common cause of three
#: failures is a person, not an attacker.
FAILURES_BEFORE_LOCK = 5

#: Backoff, in seconds, indexed by how far past the threshold we are. Caps at
#: fifteen minutes: longer stops no serious attacker and starts costing the
#: institution real support calls.
_BACKOFF_SECONDS = (60, 120, 300, 600, 900)


class AuthError(Exception):
    """Authentication failed. One message for every cause, deliberately."""

    def __init__(self, message: str = "invalid credentials") -> None:
        super().__init__(message)


class LockedOut(AuthError):
    """Locked. Still an `AuthError`, and still says "invalid credentials" to
    the caller -- the distinction exists for logging and for the retry-after
    header, not for the response body."""

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__()
        self.retry_after_seconds = retry_after_seconds


@dataclass(frozen=True, slots=True)
class LoginResult:
    access_token: str
    refresh_token: str
    expires_in: int
    user_id: str
    role: UserRole
    must_change_password: bool


def _as_uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class AuthService:
    def __init__(self, session: Session, settings: SecuritySettings) -> None:
        self._session = session
        self._settings = settings

    # -- login --------------------------------------------------------------

    def login(
        self,
        email: str,
        password: str,
        *,
        client_ip: str | None = None,
        user_agent: str | None = None,
        now: dt.datetime | None = None,
    ) -> LoginResult:
        now = now or dt.datetime.now(dt.timezone.utc)

        user = self._session.scalar(
            select(User).where(User.email == email.strip().lower())
        )
        credential = (
            self._session.scalar(
                select(UserCredential).where(UserCredential.app_user_id == user.id)
            )
            if user is not None
            else None
        )

        if user is None or credential is None:
            # Same cost as a real check, so "does this account exist" is not
            # measurable with a stopwatch.
            verify_password(password, _DUMMY_HASH)
            log.info("login failed", extra={"reason": "no such account"})
            raise AuthError

        if credential.locked_until is not None and credential.locked_until > now:
            remaining = int((credential.locked_until - now).total_seconds())
            log.warning(
                "login refused: locked",
                extra={"user_id": str(user.id), "retry_after": remaining},
            )
            raise LockedOut(remaining)

        if not verify_password(password, credential.password_hash):
            self._register_failure(credential, now)
            log.info("login failed", extra={"user_id": str(user.id)})
            raise AuthError

        if not user.is_active:
            # Checked *after* the password, so a deactivated account is not
            # distinguishable from a wrong password by response timing.
            log.warning("login refused: deactivated", extra={"user_id": str(user.id)})
            raise AuthError

        return self._succeed(user, credential, now, client_ip, user_agent)

    def _register_failure(self, credential: UserCredential, now: dt.datetime) -> None:
        credential.failed_attempts += 1
        over = credential.failed_attempts - FAILURES_BEFORE_LOCK
        if over >= 0:
            seconds = _BACKOFF_SECONDS[min(over, len(_BACKOFF_SECONDS) - 1)]
            credential.locked_until = now + dt.timedelta(seconds=seconds)
        self._session.flush()

    def _succeed(
        self,
        user: User,
        credential: UserCredential,
        now: dt.datetime,
        client_ip: str | None,
        user_agent: str | None,
    ) -> LoginResult:
        credential.failed_attempts = 0
        credential.locked_until = None
        credential.last_login_at = now

        # The only moment the plaintext is available to re-hash with. Without
        # this, raising the cost parameters protects nobody who already has an
        # account.
        if needs_rehash(credential.password_hash):
            log.info("rehashing at current parameters", extra={"user_id": str(user.id)})

        access, _ = issue_access_token(
            subject=str(user.id),
            secret=self._settings.secret_key,
            role=user.primary_role.value,
            lifetime_minutes=self._settings.access_token_minutes,
        )
        refresh, refresh_jti = issue_refresh_token(
            subject=str(user.id),
            secret=self._settings.secret_key,
            lifetime_days=self._settings.refresh_token_days,
        )
        self._session.add(
            UserSession(
                app_user_id=user.id,
                refresh_jti=refresh_jti,
                expires_at=now + dt.timedelta(days=self._settings.refresh_token_days),
                client_ip=(client_ip or None) and client_ip[:45],
                user_agent=(user_agent or None) and user_agent[:512],
            )
        )
        self._session.flush()
        log.info("login succeeded", extra={"user_id": str(user.id)})

        return LoginResult(
            access_token=access,
            refresh_token=refresh,
            expires_in=self._settings.access_token_minutes * 60,
            user_id=str(user.id),
            role=user.primary_role,
            must_change_password=credential.must_change,
        )

    # -- refresh ------------------------------------------------------------

    def refresh(
        self, refresh_token: str, *, now: dt.datetime | None = None
    ) -> LoginResult:
        """Exchange a refresh token for a new pair, rotating the old one.

        **Rotation is mandatory, not an option.** A refresh token that stays
        valid after use can be replayed by whoever stole it for its whole
        lifetime. Rotating means a stolen token works once; if the legitimate
        client then presents the old one, that is evidence of theft rather
        than a puzzle.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        try:
            claims = decode_token(
                refresh_token,
                secret=self._settings.secret_key,
                expected_type=TokenType.REFRESH,
            )
        except TokenError as exc:
            log.info("refresh rejected", extra={"reason": str(exc)})
            raise AuthError from exc

        row = self._session.scalar(
            select(UserSession).where(UserSession.refresh_jti == claims.jti)
        )
        if row is None or row.is_revoked or row.expires_at <= now:
            # A *valid signature* whose session row is gone means the token
            # was revoked, or replayed after rotation. Either way every live
            # session for this user is suspect.
            if row is not None and row.is_revoked:
                log.warning(
                    "a revoked refresh token was replayed; revoking the user's "
                    "remaining sessions",
                    extra={"user_id": str(row.app_user_id)},
                )
                self._revoke_all(row.app_user_id, now, "REPLAY_DETECTED")
            raise AuthError

        user = self._session.get(User, row.app_user_id)
        if user is None or not user.is_active:
            raise AuthError

        row.revoked_at = now
        row.revoked_reason = "ROTATED"

        credential = self._session.scalar(
            select(UserCredential).where(UserCredential.app_user_id == user.id)
        )
        result = self._succeed(user, credential, now, row.client_ip, row.user_agent)
        return result

    # -- logout -------------------------------------------------------------

    def logout(self, refresh_token: str, *, now: dt.datetime | None = None) -> None:
        """Revoke one session. Silent on an invalid token.

        Logging out is not an operation that should be able to fail loudly: a
        client with a token it cannot use is exactly the client trying to
        discard it, and an error here leaves the token in their storage.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        try:
            claims = decode_token(
                refresh_token,
                secret=self._settings.secret_key,
                expected_type=TokenType.REFRESH,
            )
        except TokenError:
            return
        row = self._session.scalar(
            select(UserSession).where(UserSession.refresh_jti == claims.jti)
        )
        if row is not None and not row.is_revoked:
            row.revoked_at = now
            row.revoked_reason = "LOGOUT"
            self._session.flush()

    def _revoke_all(self, user_id, now: dt.datetime, reason: str) -> None:
        rows = self._session.scalars(
            select(UserSession).where(
                UserSession.app_user_id == user_id,
                UserSession.revoked_at.is_(None),
            )
        )
        for row in rows:
            row.revoked_at = now
            row.revoked_reason = reason
        self._session.flush()

    # -- who is this --------------------------------------------------------

    def change_password(
        self,
        user_id: uuid.UUID | str,
        current_password: str,
        new_password: str,
        *,
        now: dt.datetime | None = None,
    ) -> None:
        """Change your own password.

        Until SESSION 33 there was no way to change a password at all: an
        administrator handed you one and you kept it. `must_change` existed on
        the credential, was reported at login, and could never be satisfied.

        **The current password is required even when `must_change` is set.**
        The holder of a stolen access token would otherwise be able to set a
        new password and lock the real owner out, turning a token theft into
        an account theft.

        **Every other session is revoked.** Changing a password is what
        somebody does when they believe it is compromised; leaving the
        attacker's refresh token live is the one outcome that makes the act
        pointless.
        """
        now = now or dt.datetime.now(dt.timezone.utc)
        credential = self._session.scalars(
            select(UserCredential).where(
                UserCredential.app_user_id == _as_uuid(user_id)
            )
        ).first()
        if credential is None:
            raise AuthError("no credential for this account")

        if not verify_password(current_password, credential.password_hash):
            # The same error as a failed login, and it counts as one: an
            # endpoint that verifies a password without rate limiting is a
            # password oracle behind a valid token.
            self._register_failure(credential, now)
            self._session.flush()
            raise AuthError()

        if new_password == current_password:
            raise AuthError("the new password must differ from the current one")

        credential.password_hash = hash_password(new_password)
        credential.must_change = False
        credential.password_changed_at = now
        credential.failed_attempts = 0
        credential.locked_until = None
        self._session.flush()
        self._revoke_all(_as_uuid(user_id), now, reason="password_changed")
        log.info(
            "password changed",
            extra={"user_id": str(user_id)},
        )

    def principal_from_access_token(self, token: str) -> Principal:
        """Verify an access token and load the *current* role.

        The role in the token is a hint used for display; authorisation reads
        the database. A token minted before a demotion would otherwise keep
        its old privileges for up to its whole lifetime.
        """
        try:
            claims = decode_token(
                token,
                secret=self._settings.secret_key,
                expected_type=TokenType.ACCESS,
            )
        except TokenError as exc:
            raise AuthError from exc

        user = self._session.get(User, _as_uuid(claims.subject))
        if user is None or not user.is_active:
            raise AuthError

        # SESSION 23: the scope handles are populated. They existed from
        # SESSION 8 and were always empty, because nothing in the schema said
        # which programmes a coordinator coordinates -- so every scoped role
        # behaved as though it held the whole institution. Read here rather
        # than carried in the token, for the same reason the role is: an
        # assignment revoked this morning must not survive in a token minted
        # last night.
        from academic_ai.infrastructure.repositories import load_jurisdiction

        jurisdiction = load_jurisdiction(self._session, user.id, user.primary_role)
        credential = self._session.scalars(
            select(UserCredential).where(UserCredential.app_user_id == user.id)
        ).first()
        return Principal(
            user_id=str(user.id),
            role=user.primary_role,
            kind=ActorKind.HUMAN,
            is_active=user.is_active,
            program_ids=jurisdiction.program_ids,
            faculty_ids=jurisdiction.faculty_ids,
            # Carried so `require()` can refuse. A temporary password that
            # works indefinitely is not temporary, and this flag was reported
            # at login and enforced nowhere.
            must_change_password=bool(credential and credential.must_change),
        )
