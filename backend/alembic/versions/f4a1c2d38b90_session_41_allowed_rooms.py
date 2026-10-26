"""SESSION 41: which rooms a class may use.

`SessionSpec.allowed_room_ids` has existed since SESSION 2 and the FET reader
has computed it since the FET work — from `ConstraintSubjectPreferredRooms`
and `ConstraintActivityPreferredRoom(s)`, 226 of them in the file this project
imports, restricting **374 of 399 sessions**. There was nowhere in the schema
to put it, so every one of them was dropped on import and `compute_domains`
offered every room for every class.

**Empty means unrestricted.** No rows for a session is the common case and
says "anywhere"; it is not the same as a session that may use nothing.

Revision ID: f4a1c2d38b90
Revises: e2c918a4bd71
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f4a1c2d38b90"
down_revision: str | None = "e2c918a4bd71"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "class_session_allowed_room",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("class_session_id", sa.UUID(), nullable=False),
        sa.Column("room_id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["class_session_id"],
            ["class_session.id"],
            name=op.f("fk_class_session_allowed_room_class_session_id_class_session"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["room_id"],
            ["room.id"],
            name=op.f("fk_class_session_allowed_room_room_id_room"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_class_session_allowed_room")),
        # The same room listed twice for one session would double its weight
        # in any count of how restricted a class is.
        sa.UniqueConstraint(
            "class_session_id",
            "room_id",
            name="uq_class_session_allowed_room_session_room",
        ),
    )
    for column in ("class_session_id", "room_id"):
        op.create_index(
            f"ix_class_session_allowed_room_{column}",
            "class_session_allowed_room",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for column in ("room_id", "class_session_id"):
        op.drop_index(
            f"ix_class_session_allowed_room_{column}",
            table_name="class_session_allowed_room",
        )
    op.drop_table("class_session_allowed_room")
