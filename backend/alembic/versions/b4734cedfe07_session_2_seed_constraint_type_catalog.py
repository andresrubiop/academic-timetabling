"""SESSION 2: seed constraint_type catalog

Revision ID: b4734cedfe07
Revises: 13569e7ea0e8
Create Date: 2026-08-14

Seeds `constraint_type` with the SESSION 2 classification.

The rows below are a **frozen snapshot** of
`academic_ai.domain.scheduling.catalog.CONSTRAINT_CATALOG` as it stood when
this migration was written. They are deliberately not imported from that
module: a migration that read live application code would produce different
data every time the catalog changed, so replaying history would not reproduce
history, and the divergence test below could never fail.

`tests/test_constraint_catalog.py` compares the seeded rows against the live
catalog and fails if they differ — the same contract `alembic check` provides
for schema, applied to data. Changing a classification therefore requires a
new migration, which is correct: a constraint's category is an institutional
decision with an audit trail, not a constant somebody edits.

`is_builtin` is TRUE for every row here. A builtin may not be deleted through
the API; SESSION 14 must enforce that when it builds the CRUD surface.
"""
from __future__ import annotations

import json
from typing import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = 'b4734cedfe07'
down_revision: str | None = '13569e7ea0e8'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_SEED_ROWS: tuple[dict[str, object], ...] = (
    {
        'code': 'SESSION_WITHIN_DAY_GRID',
        'name': 'Session fits inside one day of the grid',
        'description': 'A session occupies duration_slots consecutive in-day slots, all of which must exist in the same day.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'SESSION_SLOTS_SCHEDULABLE',
        'name': 'Session occupies only schedulable slots',
        'description': 'Every slot a session occupies must have is_schedulable = true. Blocked bands (lunch, assembly) are dense rows, not holes.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'SESSION_ROOM_PRESENCE',
        'name': 'Room assigned exactly when required',
        'description': 'A session with requires_room must receive a room; one without must not receive one.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'SECTION_SESSIONS_NO_OVERLAP',
        'name': 'Sessions of one section do not overlap',
        'description': 'Two ClassSessions of the same Section may not occupy overlapping slots. A section is one group of students; its lecture and its lab are attended by the same bodies.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'TEACHER_NO_OVERLAP',
        'name': 'A teacher is in one place at a time',
        'description': 'Two sessions sharing a time-occupying teacher may not overlap.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'ROOM_NO_OVERLAP',
        'name': 'A room holds one class at a time',
        'description': 'Two sessions in the same room may not overlap, unless they belong to the same co-teaching group, in which case they are one class being taught once.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'ROOM_FEATURE_REQUIREMENT',
        'name': 'Room provides the required capabilities',
        'description': 'For every SessionRequiredFeature, the assigned room must provide that feature in at least the required quantity.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'ROOM_ACTIVE',
        'name': 'Only active rooms may be used',
        'description': 'A room with lifecycle status other than ACTIVE is unusable.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'CO_TEACHING_SAME_TIMESLOT',
        'name': 'Co-taught sessions start together',
        'description': 'All members of a SessionCoTeachingGroup share one start slot.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'CO_TEACHING_SAME_ROOM',
        'name': 'Co-taught sessions share a room',
        'description': 'All members of a SessionCoTeachingGroup share one room.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'TEACHER_CAMPUS_TRAVEL',
        'name': 'A teacher can physically reach their next class',
        'description': 'Consecutive sessions of one teacher on one day, in rooms on different campuses, must be separated by at least the declared travel time.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'TEACHER_AVAILABILITY',
        'name': 'Teacher declared unavailability is respected',
        'description': 'A session may not occupy a slot where one of its time-occupying teachers has an UNAVAILABLE Availability row.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'ROOM_AVAILABILITY',
        'name': 'Room declared unavailability is respected',
        'description': 'A session may not occupy a slot where its room has an UNAVAILABLE Availability row.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'ASSIGNMENT_LOCKED',
        'name': 'Locked assignments do not move',
        'description': 'An assignment carried over from the base version with is_locked = true keeps its slot and room.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'SESSION_PINNED',
        'name': 'Durably pinned session',
        'description': 'A session pinned by institutional decision keeps the slot and room named in the constraint parameters, across versions.',
        'default_category': 'HARD',
        'is_category_overridable': False,
        'parameter_schema': {'additionalProperties': False, 'anyOf': [{'required': ['timeslot_id']}, {'required': ['room_id']}], 'properties': {'room_id': {'format': 'uuid', 'type': 'string'}, 'timeslot_id': {'format': 'uuid', 'type': 'string'}}, 'type': 'object'},
        'parameter_schema_version': '1',
    },
    {
        'code': 'ROOM_CAPACITY',
        'name': 'Room is big enough',
        'description': "Assigned room capacity must be at least the session's seat demand: the larger of the section's size and its declared minimum, summed across a co-teaching group.",
        'default_category': 'HARD',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'ROOM_CAMPUS_MATCH',
        'name': "Room is on the offering's campus",
        'description': "A session's room must be on the campus its CourseOffering names.",
        'default_category': 'HARD',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'ROOM_TYPE_ALLOWED',
        'name': 'Room type suits the session',
        'description': "The assigned room's type must be in the allowed set for the session's type and delivery mode.",
        'default_category': 'HARD',
        'is_category_overridable': True,
        'parameter_schema': {'additionalProperties': False, 'properties': {'allowed_room_types': {'items': {'type': 'string'}, 'minItems': 1, 'type': 'array'}}, 'required': ['allowed_room_types'], 'type': 'object'},
        'parameter_schema_version': '1',
    },
    {
        'code': 'STUDENT_NO_OVERLAP_UNRESOLVABLE',
        'name': 'A student with no alternative is never double-booked',
        'description': 'Two sessions may not overlap when some student holds active mandatory enrollments in both and neither can be moved to another section, so no action by anyone resolves the clash.',
        'default_category': 'HARD',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'STUDENT_CAMPUS_TRAVEL',
        'name': 'A student can physically reach their next class',
        'description': 'Consecutive sessions of one student on one day, on different campuses, must be separated by at least the declared travel time.',
        'default_category': 'HARD',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'STUDENT_NO_OVERLAP_MANDATORY',
        'name': 'Mandatory student conflicts are minimised',
        'description': 'Two sessions overlapping while a student holds active mandatory enrollments in both, where the student could in principle move to another section.',
        'default_category': 'MEDIUM',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'STUDENT_NO_OVERLAP_ELECTIVE',
        'name': 'Elective student conflicts are minimised',
        'description': 'As above, where at least one of the two enrollments is not mandatory.',
        'default_category': 'MEDIUM',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'TEACHER_MAX_DAILY_HOURS',
        'name': 'Teacher daily teaching limit',
        'description': 'Total teaching minutes for a teacher on one day may not exceed their max_daily_hours.',
        'default_category': 'MEDIUM',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'TEACHER_MAX_CONSECUTIVE_SLOTS',
        'name': 'Teacher consecutive-teaching limit',
        'description': 'A teacher may not teach more than N consecutive slots without a break.',
        'default_category': 'MEDIUM',
        'is_category_overridable': True,
        'parameter_schema': {'additionalProperties': False, 'properties': {'max_consecutive_slots': {'exclusiveMinimum': 0, 'type': 'integer'}}, 'required': ['max_consecutive_slots'], 'type': 'object'},
        'parameter_schema_version': '1',
    },
    {
        'code': 'TEACHER_MAX_TEACHING_DAYS',
        'name': 'Teacher weekly distribution',
        'description': "A teacher's sessions should occupy at most N distinct days.",
        'default_category': 'MEDIUM',
        'is_category_overridable': True,
        'parameter_schema': {'additionalProperties': False, 'properties': {'max_teaching_days': {'exclusiveMinimum': 0, 'type': 'integer'}}, 'required': ['max_teaching_days'], 'type': 'object'},
        'parameter_schema_version': '1',
    },
    {
        'code': 'STUDENT_MAX_DAILY_HOURS',
        'name': 'Student daily load limit',
        'description': 'Total class minutes for a student on one day may not exceed the configured limit.',
        'default_category': 'MEDIUM',
        'is_category_overridable': True,
        'parameter_schema': {'additionalProperties': False, 'properties': {'max_daily_minutes': {'exclusiveMinimum': 0, 'type': 'integer'}}, 'required': ['max_daily_minutes'], 'type': 'object'},
        'parameter_schema_version': '1',
    },
    {
        'code': 'SECTION_DAY_SPREAD',
        'name': "A section's sessions are spread across the week",
        'description': 'The sessions of one section should fall on distinct days, and at most max_sessions_per_day of them may share a day.',
        'default_category': 'MEDIUM',
        'is_category_overridable': True,
        'parameter_schema': {'additionalProperties': False, 'properties': {'max_sessions_per_day': {'exclusiveMinimum': 0, 'type': 'integer'}, 'min_distinct_days': {'exclusiveMinimum': 0, 'type': 'integer'}}, 'type': 'object'},
        'parameter_schema_version': '1',
    },
    {
        'code': 'SCHEDULE_STABILITY',
        'name': 'Minimal disruption to the base version',
        'description': 'The number of assignments whose slot or room differs from the base schedule version.',
        'default_category': 'MEDIUM',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'TEACHER_MAX_WEEKLY_HOURS',
        'name': 'Teacher weekly workload limit',
        'description': 'Total teaching minutes assigned to a teacher across the week may not exceed their max_weekly_hours.',
        'default_category': 'MEDIUM',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'TEACHER_GAP_MINIMISATION',
        'name': 'Teacher idle gaps are minimised',
        'description': "Idle slots between a teacher's first and last session on a day.",
        'default_category': 'SOFT',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'STUDENT_GAP_MINIMISATION',
        'name': 'Student idle gaps are minimised',
        'description': "Idle slots between a student's first and last session on a day.",
        'default_category': 'SOFT',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'ROOM_CHANGE_MINIMISATION',
        'name': 'Room changes are minimised',
        'description': 'Consecutive sessions of the same section, or of the same student, placed in different rooms or buildings.',
        'default_category': 'SOFT',
        'is_category_overridable': True,
        'parameter_schema': {'additionalProperties': False, 'properties': {'penalise_building_change': {'type': 'boolean'}, 'penalise_room_change': {'type': 'boolean'}}, 'type': 'object'},
        'parameter_schema_version': '1',
    },
    {
        'code': 'ROOM_CAPACITY_FIT',
        'name': 'Rooms are not wastefully oversized',
        'description': 'Unused seats when a session is placed in a room much larger than its demand.',
        'default_category': 'SOFT',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'TEACHER_PREFERENCE',
        'name': 'Teacher preferences are satisfied',
        'description': 'Violations of Preference rows owned by a teacher: preferred and avoided slots, days, rooms and campuses, early/late bands, compactness and daily-hour wishes.',
        'default_category': 'SOFT',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'PROGRAM_PREFERENCE',
        'name': 'Programme preferences are satisfied',
        'description': 'Violations of Preference rows owned by a programme.',
        'default_category': 'SOFT',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
    {
        'code': 'COHORT_PREFERENCE',
        'name': 'Cohort preferences are satisfied',
        'description': 'Violations of Preference rows owned by a student cohort.',
        'default_category': 'SOFT',
        'is_category_overridable': True,
        'parameter_schema': None,
        'parameter_schema_version': '1',
    },
)

def upgrade() -> None:
    connection = op.get_bind()
    statement = sa.text(
        """
        INSERT INTO constraint_type (
            code, name, description, default_category,
            is_category_overridable, parameter_schema,
            parameter_schema_version, is_builtin
        ) VALUES (
            :code, :name, :description,
            CAST(:default_category AS constraint_category),
            :is_category_overridable,
            CAST(:parameter_schema AS jsonb),
            :parameter_schema_version, TRUE
        )
        """
    )
    for row in _SEED_ROWS:
        schema = row["parameter_schema"]
        connection.execute(
            statement,
            {
                **row,
                "parameter_schema": None if schema is None else json.dumps(schema),
            },
        )


def downgrade() -> None:
    # Only the builtin rows this migration created. An institution's own
    # constraint types are not this migration's to delete.
    op.get_bind().execute(
        sa.text(
            "DELETE FROM constraint_type "
            "WHERE is_builtin AND code = ANY(:codes)"
        ),
        {"codes": [row["code"] for row in _SEED_ROWS]},
    )
