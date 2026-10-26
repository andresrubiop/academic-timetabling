"""Credentials and sessions (SESSION 8).

Separate tables rather than columns on `app_user`, for three reasons that all
turn out to be the same reason: a password hash has a different lifecycle from
the identity it belongs to.

* `app_user` is referenced by audit entries, approvals and AI-model
  activations, so it is read constantly and by almost everything. A hash
  living there is a hash that gets selected into memory by every listing
  screen that does `SELECT *`.
* Credentials are replaced, locked and expired on their own schedule. An
  account is not deleted when its password is.
* MFA secrets and recovery codes arrive later and belong beside the password,
  not beside the person's name.

`user_session` exists so that logging out means something. An access token is
signed and stateless and cannot be withdrawn, which is why it lives for
minutes; the week-long refresh token is a handle to a row here, and revoking
it is a `DELETE`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from academic_ai.infrastructure.db.base import (
    ActorMixin,
    Base,
    MetadataMixin,
    TimestampMixin,
    fk_column,
    pk_column,
)

if TYPE_CHECKING:
    from academic_ai.infrastructure.db.models.people import User


class UserCredential(Base, TimestampMixin, MetadataMixin):
    """One password, and the state that decides whether it may be used."""

    __tablename__ = "user_credential"
    __table_args__ = (
        UniqueConstraint("app_user_id", name="uq_user_credential_app_user_id"),
        # An argon2id hash is `$argon2id$v=...$m=...$...$salt$hash`. The check
        # is a guard against somebody storing a plaintext password in this
        # column, which is the single worst thing that could happen here and
        # which no amount of review reliably catches.
        CheckConstraint(
            "password_hash LIKE '$argon2%'",
            name="password_hash_is_argon2",
        ),
        CheckConstraint(
            "failed_attempts >= 0", name="failed_attempts_not_negative"
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    app_user_id: Mapped[uuid.UUID] = fk_column("app_user.id", ondelete="CASCADE")
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    #: Set when an administrator issues a temporary password. The API refuses
    #: everything except changing it until this is cleared.
    must_change: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    failed_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    #: Set by the lockout policy. Nullable rather than a sentinel date: "not
    #: locked" and "locked until 1970" must not be the same value.
    locked_until: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_login_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_changed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped[User] = relationship(foreign_keys=[app_user_id])


class UserSession(Base, TimestampMixin, MetadataMixin):
    """One refresh token, revocable.

    The token itself is **not** stored — only its `jti`. A stolen database
    backup then yields no usable credential, which is the same reasoning that
    makes the password column a hash.
    """

    __tablename__ = "user_session"
    __table_args__ = (
        UniqueConstraint("refresh_jti", name="uq_user_session_refresh_jti"),
        CheckConstraint(
            "expires_at > created_at", name="session_expires_after_it_starts"
        ),
        # The lookup on every refresh: this user's sessions that are still
        # live. Partial, because expired and revoked rows are kept for audit
        # and would otherwise dominate the index.
        Index(
            "ix_user_session_active",
            "app_user_id",
            postgresql_where=text("revoked_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = pk_column()
    app_user_id: Mapped[uuid.UUID] = fk_column("app_user.id", ondelete="CASCADE")
    #: The `jti` claim of the refresh token. Never the token.
    refresh_jti: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: Why it ended: LOGOUT, ROTATED, ADMIN_REVOKED, PASSWORD_CHANGED.
    revoked_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    #: Truncated at 45 chars — an IPv6 address — and recorded for audit. Not
    #: used for authorisation: a session pinned to an IP breaks every user on
    #: a mobile network and stops no attacker who has the token.
    client_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    user: Mapped[User] = relationship(foreign_keys=[app_user_id])

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class UserScopeAssignment(Base, TimestampMixin, ActorMixin, MetadataMixin):
    """Which faculty or programme a user is responsible for.

    The table that was missing. `Principal` has carried `program_ids` and
    `faculty_ids` since SESSION 8 and nothing ever filled them, because nothing
    in the schema said which programmes a coordinator coordinates — so
    authorisation could only ever answer "may this *kind* of thing happen",
    and every scoped role behaved as though it held the whole institution.
    SESSION 18's approval routing recorded the same absence from the other
    side: it errs deliberately wide, asking every coordinator to sign, because
    narrowing to the affected programmes needed exactly this mapping.

    One row per (user, target). A user with three programmes has three rows
    rather than an array column: a foreign key per row is what makes the
    database refuse an assignment to a programme that does not exist, and an
    array of uuids refuses nothing.

    Faculty and programme are separate nullable columns with a
    ``num_nonnulls`` check, the same shape `Availability` uses for its
    polymorphic owner. An ``(scope_kind, scope_id)`` pair would need the
    application to remember which table the id points at, and nothing would
    stop a faculty id being stored as a programme.
    """

    __tablename__ = "user_scope_assignment"
    __table_args__ = (
        CheckConstraint(
            "num_nonnulls(faculty_id, program_id) = 1",
            name="exactly_one_scope_target",
        ),
        # A user assigned the same programme twice is a data-entry mistake, and
        # it would double that programme's weight in any future count of who
        # is responsible for what. Partial, because only one of the two columns
        # is populated per row.
        Index(
            "uq_user_scope_assignment_faculty",
            "app_user_id",
            "faculty_id",
            unique=True,
            postgresql_where=text("faculty_id IS NOT NULL"),
        ),
        Index(
            "uq_user_scope_assignment_program",
            "app_user_id",
            "program_id",
            unique=True,
            postgresql_where=text("program_id IS NOT NULL"),
        ),
        # No explicit index on `app_user_id`: `fk_column` already creates
        # one, and declaring it twice makes the models disagree with any
        # migration generated from them.
    )

    id: Mapped[uuid.UUID] = pk_column()
    app_user_id: Mapped[uuid.UUID] = fk_column("app_user.id", ondelete="CASCADE")
    #: Exactly one of these is set.
    faculty_id: Mapped[uuid.UUID | None] = fk_column(
        "faculty.id", nullable=True, ondelete="CASCADE"
    )
    program_id: Mapped[uuid.UUID | None] = fk_column(
        "program.id", nullable=True, ondelete="CASCADE"
    )
    #: Why this person holds this scope — "Coordinadora de Sistemas desde
    #: 2026-03". Free text, and worth having: an assignment nobody can explain
    #: is one nobody dares remove.
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
