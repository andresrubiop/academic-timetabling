# ADR-0007: Scenarios as declarative overlays; schedule version immutability

Date: 2026-08-07
Status: Accepted
Session: SESSION 1

## Context

SESSION 10 creates a `Scenario` on *every drag-and-drop*, and SESSION 11
requires isolated what-if planning with minimal-disruption reoptimisation.

Half the perturbations the playbook lists — "teacher unavailable", "room
unavailable", "new section", "change room requirement", "change campus
availability" — mutate **base tables**, not assignments. A scenario that only
materialised a draft `ScheduleVersion` would have nowhere to put a
hypothetical "this teacher is ill", except the real `availability` table.

There are only two ways out: scenario-scope every mutable table (invasive and
permanent), or make the problem builder accept an overlay. That choice must be
made *before* SESSION 3 exists, because a problem builder that reads the
database directly cannot be given an overlay later without being rewritten.

Separately, `perturbations JSONB` was opaque. SESSION 10 must report affected
teachers, rooms and programmes *before* solving, and SESSION 12 must convert a
scenario into a `ChangeRequest` with an impact analysis. Neither is possible
against untyped JSON with no foreign keys.

## Decision

1. **A scenario is a declarative overlay. Base tables are never
   scenario-scoped.** The rule, binding on SESSION 2 and 3: *the problem
   builder's input is `(base data, overlay)`, never the database alone.*
2. `ScenarioPerturbation` is a typed table with real foreign-key targets
   (`class_session`, `section`, `course_offering`, `teacher`, `room`,
   `campus`, `timeslot`), an ordered `sequence_number`, a `kind` constrained
   to a known list, and a `payload` holding only non-reference values.
3. `Scenario.result_schedule_version_id` holds the DRAFT version produced by
   solving it, so diffing a scenario against its base reuses the same
   ScheduleDiff path as any other pair of versions.
4. **Schedule versions beyond DRAFT are immutable**, enforced by the
   `schedule_assignment_version_is_mutable` trigger on INSERT, UPDATE and
   DELETE — not by convention.
5. Room double-booking is impossible by construction:
   `EXCLUDE USING gist (schedule_version_id =, room_id =, day_of_week =,
   int4range(start_slot_index, start_slot_index + duration_slots) &&)
   WHERE room_id IS NOT NULL`.

## Consequences

- Room double-booking is structurally impossible rather than validator-detected.
  Teacher and student overlap remain validator concerns (SESSION 3): they need
  joins through `session_teacher` and `enrollment` and cannot be a
  single-table exclusion.
- Merging and unmerging offerings, moving sessions, and marking a teacher
  unavailable are all scenario-safe and leave no residue.
- The overlay requirement constrains the SESSION 2/3 API design. That is the
  point.

## Open item before SESSION 10

Scenario-derived DRAFT versions will be created one per drag-and-drop. A
retention/GC policy is needed before SESSION 10 ships, given the 14 GiB of
free space on the target host recorded in `ARCHITECTURE.md` §4.

## Tests

`backend/tests/test_schedule.py::TestRoomOccupancy` — a room cannot be
occupied twice; non-overlapping times and different rooms are fine.
`backend/tests/test_schedule.py::TestVersionImmutability` — draft assignments
are editable; published ones cannot be added, changed or deleted.
