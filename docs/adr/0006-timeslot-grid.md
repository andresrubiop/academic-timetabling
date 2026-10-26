# ADR-0006: Timeslot grid granularity, density and period scoping

Date: 2026-08-07
Status: Accepted
Session: SESSION 1

## Context

The timeslot grid is the most irreversible constant in the platform. Every
availability row, every session duration and every schedule assignment is
denominated in slots. Changing it later is not a migration, it is a re-plan.

The first design had atomic uniform slots with a session occupying
`duration_slots` consecutive slots — correct in spirit, but with three gaps
architecture review identified:

1. **"Consecutive slots" is not "contiguous time"** if breaks are modelled by
   *omitting* slot rows. A three-slot class starting before lunch would jump
   the gap and silently straddle it. The same arithmetic straddles a day
   boundary if the slot index is global across the week.
2. **Slot length was nowhere defined**, yet it is the value that decides
   whether a 45-minute class is expressible at all.
3. `Availability` carried a redundant, unbound `academic_period_id` alongside a
   period-scoped timeslot.

## Decision

- Slots are **atomic and uniform**; a longer session occupies
  `duration_slots` consecutive slots.
- `AcademicPeriod.slot_minutes` fixes the granularity **per period**, with
  `CHECK (slot_minutes > 0 AND mod(1440, slot_minutes) = 0)`. Institutions
  change their bell schedule between periods, never within one.
- The grid is **dense**: every slot of the working day exists as a row. Breaks
  are `is_schedulable = false`, not missing rows.
- Two indices are stored:
  - `day_slot_index` — dense *within one day*, starting at 0. **Occupancy
    arithmetic uses this one**, so a session cannot straddle a day boundary:
    the arithmetic simply runs out of slots.
  - `global_slot_index` — dense across the week, for ordering and week-level
    features (teacher gaps, compactness).
- Slots within a period and day may not overlap:
  `EXCLUDE USING gist (academic_period_id =, day_of_week =,
  int4range(start_minute, end_minute) &&)`. This requires the `btree_gist`
  extension, created by the baseline migration.
- `Availability` and `Preference` reference the timeslot; the redundant period
  column on `Availability` was removed.
- `ScheduleAssignment` denormalises `(academic_period_id, day_of_week,
  start_slot_index, duration_slots, requires_room)`, each bound by composite
  foreign key, so room-occupancy exclusion is a pure index operation.

## Consequences

- Row counts stay trivial (a dense week at 60-minute granularity is a few
  hundred rows per period).
- CP-SAT gets clean integer intervals over `day_slot_index`.
- "No class over lunch" becomes *data*, not code — which SESSION 2 wants
  anyway.
- A period's grid must be created before its offerings can be scheduled.
  SESSION 14/15 should offer "clone grid and availability from a previous
  period".

## Alternatives considered

- **Sparse grid with interval arithmetic on minutes.** More flexible, but it
  forfeits the integer-index advantage CP-SAT depends on and complicates
  availability into range queries.
- **A shared reusable `TimeGrid` referenced by several periods.** Deferred:
  period-scoped is simpler and matches how the playbook scopes everything else.

## Open item for SESSION 2

The grid must be validated against the sessions that use it: a period with
`slot_minutes = 60` cannot express a 45-minute session, and that must surface
as a named diagnostic, not a rounding.

## Tests

`backend/tests/test_facilities_and_period.py::TestTimeslotGrid` — slot_minutes
must divide a day; overlapping slots on the same day are rejected; the same
times on a different day are fine; the grid is dense and lunch is a blocked
slot rather than a hole.
