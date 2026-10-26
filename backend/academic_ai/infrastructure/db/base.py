"""Declarative base, naming conventions, shared column types and mixins.

Naming convention matters more than usual here: Alembic autogenerate can only
produce stable, reversible migrations if every index and constraint has a
deterministic name. Without this, `alembic revision --autogenerate` emits
`None` constraint names and downgrades break.
"""

from __future__ import annotations

import datetime as dt
import enum
import uuid
from typing import Any

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    MetaData,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Base(DeclarativeBase):
    metadata = metadata

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


# ---------------------------------------------------------------------------
# Column helpers
# ---------------------------------------------------------------------------

#: Primary/foreign key type. UUIDs are generated database-side so that rows
#: created by the optimizer worker, an import job, or the API are all
#: consistent without a shared sequence.
UUIDType = PGUUID(as_uuid=True)


def pg_enum(python_enum: type[enum.Enum], name: str) -> SAEnum:
    """Map a domain enum onto a native PostgreSQL enum type.

    ``values_callable`` persists the *value* rather than the *name*. Our
    domain enums keep value == name, but being explicit stops a future
    rename from silently rewriting stored data.

    Adding a member later requires ``ALTER TYPE ... ADD VALUE`` in a
    migration — see DATABASE.md, "Evolving an enum".
    """
    return SAEnum(
        python_enum,
        name=name,
        native_enum=True,
        create_constraint=False,
        validate_strings=True,
        values_callable=lambda e: [member.value for member in e],
    )


def pk_column() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUIDType,
        primary_key=True,
        server_default=text("gen_random_uuid()"),
        default=uuid.uuid4,
    )


def fk_column(
    target: str,
    *,
    nullable: bool = False,
    ondelete: str = "RESTRICT",
    index: bool = True,
    use_alter: bool = False,
    **kwargs: Any,
) -> Mapped[uuid.UUID]:
    """Foreign key column.

    ``ondelete`` defaults to RESTRICT on purpose: academic records are
    retired, not deleted (DATABASE.md, "Lifecycles and deletion policy"), so
    an accidental cascade is a data-loss bug rather than a convenience.
    Children that are genuinely owned by their parent pass CASCADE
    explicitly.

    ``use_alter`` adds the constraint in a separate ``ALTER TABLE`` after
    both tables exist. It is needed only to break a genuine reference cycle
    between two tables that legitimately point at each other.
    """
    return mapped_column(
        UUIDType,
        ForeignKey(target, ondelete=ondelete, use_alter=use_alter),
        nullable=nullable,
        index=index,
        **kwargs,
    )


def code_column(length: int = 32, **kwargs: Any) -> Mapped[str]:
    """Short human-facing identifier (course code, room code, NRC, ...)."""
    return mapped_column(String(length), nullable=False, **kwargs)


def name_column(length: int = 255, **kwargs: Any) -> Mapped[str]:
    return mapped_column(String(length), nullable=False, **kwargs)


# ---------------------------------------------------------------------------
# Mixins
# ---------------------------------------------------------------------------


class TimestampMixin:
    """Server-side timestamps.

    Defaults are server-side so that rows written by Alembic, by a bulk
    import, or by the optimizer worker are timestamped consistently rather
    than by whichever Python process happened to write them.
    """

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class ActorMixin:
    """Who created / last modified the row.

    Nullable because rows can legitimately originate from a migration, a
    seed, or an automated import with no interactive user. The authoritative
    change history lives in ``audit_log``; these columns are a fast path for
    listing screens, not a substitute for it.
    """

    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    updated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("app_user.id", ondelete="SET NULL"),
        nullable=True,
    )


class MetadataMixin:
    """Free-form extension point for institution-specific attributes.

    Named ``extra`` rather than ``metadata`` because ``metadata`` is reserved
    by SQLAlchemy's declarative API.
    """

    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
