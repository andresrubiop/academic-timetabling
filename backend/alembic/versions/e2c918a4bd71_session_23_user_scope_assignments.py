"""SESSION 23: user scope assignments.

The table that lets authorisation answer *where*.

`Principal` has carried `program_ids` and `faculty_ids` since SESSION 8 and
nothing ever populated them, because nothing in the schema recorded which
programmes a coordinator coordinates. Every scoped role therefore behaved as
though it held the whole institution. SESSION 18's approval routing recorded
the same absence from the other side: it asks *every* coordinator to sign a
`requires_all_affected` step, because narrowing to the affected programmes
needed exactly this mapping.

**This migration adds no access. Applying it and assigning nobody leaves every
scoped role holding nothing** — which is the correct default and a real
operational step, not an accident. See SECURITY.md.

Revision ID: e2c918a4bd71
Revises: cc47b35fffe9
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e2c918a4bd71"
down_revision: str | None = "cc47b35fffe9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_scope_assignment",
        sa.Column(
            "id",
            sa.UUID(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("app_user_id", sa.UUID(), nullable=False),
        sa.Column("faculty_id", sa.UUID(), nullable=True),
        sa.Column("program_id", sa.UUID(), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
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
        sa.Column("created_by_user_id", sa.UUID(), nullable=True),
        sa.Column("updated_by_user_id", sa.UUID(), nullable=True),
        sa.Column(
            "extra",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        # Exactly one target. An (scope_kind, scope_id) pair would leave the
        # application to remember which table the id points at, and nothing
        # would stop a faculty id being stored as a programme.
        sa.CheckConstraint(
            "num_nonnulls(faculty_id, program_id) = 1",
            name=op.f("ck_user_scope_assignment_exactly_one_scope_target"),
        ),
        sa.ForeignKeyConstraint(
            ["app_user_id"],
            ["app_user.id"],
            name=op.f("fk_user_scope_assignment_app_user_id_app_user"),
            ondelete="CASCADE",
        ),
        # CASCADE on the target too: a deleted programme must not leave an
        # assignment pointing at nothing, which would read as "assigned" in
        # any audit of who holds what.
        sa.ForeignKeyConstraint(
            ["faculty_id"],
            ["faculty.id"],
            name=op.f("fk_user_scope_assignment_faculty_id_faculty"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["program_id"],
            ["program.id"],
            name=op.f("fk_user_scope_assignment_program_id_program"),
            ondelete="CASCADE",
        ),
        # ActorMixin: who created and last touched the assignment. Handing
        # somebody authority over a faculty is exactly the kind of act an
        # audit needs a name against.
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_user_scope_assignment_created_by_user_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["app_user.id"],
            name=op.f("fk_user_scope_assignment_updated_by_user_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_scope_assignment")),
    )
    # `fk_column` indexes every foreign key: an unindexed FK makes the
    # parent's DELETE scan this table, and CASCADE means that happens on every
    # programme deletion.
    for column in (
        "app_user_id",
        "faculty_id",
        "program_id",
        # `created_by_user_id` only: `ActorMixin` indexes who made a row, not
        # who last touched it, because the first is what an audit query filters
        # on and the second is read one row at a time.
        "created_by_user_id",
    ):
        op.create_index(
            f"ix_user_scope_assignment_{column}",
            "user_scope_assignment",
            [column],
            unique=False,
        )
    # Partial uniques, because only one of the two columns is populated per
    # row. A user assigned the same programme twice would double that
    # programme's weight in any count of who is responsible for it.
    op.create_index(
        "uq_user_scope_assignment_faculty",
        "user_scope_assignment",
        ["app_user_id", "faculty_id"],
        unique=True,
        postgresql_where=sa.text("faculty_id IS NOT NULL"),
    )
    op.create_index(
        "uq_user_scope_assignment_program",
        "user_scope_assignment",
        ["app_user_id", "program_id"],
        unique=True,
        postgresql_where=sa.text("program_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Dropping this table removes every scope assignment. That is data loss in
    # the ordinary sense -- who was responsible for what -- and it is why the
    # table carries `note`: a downgrade should be preceded by a dump, and the
    # notes are what make the dump readable afterwards.
    op.drop_index(
        "uq_user_scope_assignment_program", table_name="user_scope_assignment"
    )
    op.drop_index(
        "uq_user_scope_assignment_faculty", table_name="user_scope_assignment"
    )
    for column in (
        "created_by_user_id",
        "program_id",
        "faculty_id",
        "app_user_id",
    ):
        op.drop_index(
            f"ix_user_scope_assignment_{column}", table_name="user_scope_assignment"
        )
    op.drop_table("user_scope_assignment")
