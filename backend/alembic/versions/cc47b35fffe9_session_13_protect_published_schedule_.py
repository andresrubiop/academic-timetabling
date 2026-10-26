"""SESSION 13: a published schedule version cannot be deleted

Found by the architecture checkpoint, and reproduced before it was fixed:

    DELETE FROM schedule_version WHERE status = 'PUBLISHED';

succeeded, and cascaded away every ``schedule_assignment`` belonging to it.
The published timetable -- the one students have already read and arranged
their week around -- could be removed by one statement with no error.

The existing ``schedule_assignment_version_is_mutable`` trigger did not stop
it, and could not: it looks up the version's status, and during a cascading
delete that row is already gone, so it takes the ``v_status IS NULL`` branch
and steps aside. That branch is correct for its own purpose. The gap was that
nothing guarded the *version* itself.

CLAUDE.md states the policy this restores: academic records are retired, not
deleted. A version that should no longer be in force gets ``ARCHIVED``; the
history stays, because "what was the timetable in March" is a question the
institution will be asked and cannot answer from a deleted row.

DRAFT versions remain deletable. A draft is working material, it was never in
force, and forcing an operator to archive scratch work would leave every
period cluttered with versions nobody wants -- which is how a rule like this
gets dropped.
"""

from __future__ import annotations

from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = 'cc47b35fffe9'
down_revision: str | None = '91936fe7eeb2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None



_TRIGGER_SQL = """
CREATE OR REPLACE FUNCTION schedule_version_is_deletable()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.status::text <> 'DRAFT' THEN
        RAISE EXCEPTION
            'schedule_version % is % and cannot be deleted; archive it instead. '
            'Deleting it would remove the timetable people have already read, '
            'and its assignments with it.', OLD.id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_schedule_version_is_deletable
    BEFORE DELETE ON schedule_version
    FOR EACH ROW EXECUTE FUNCTION schedule_version_is_deletable();
"""

_DROP_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS trg_schedule_version_is_deletable ON schedule_version;
DROP FUNCTION IF EXISTS schedule_version_is_deletable();
"""


def upgrade() -> None:
    op.execute(_TRIGGER_SQL)


def downgrade() -> None:
    op.execute(_DROP_TRIGGER_SQL)
