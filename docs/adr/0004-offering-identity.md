# ADR-0004: Offering identity, co-taught offerings, and the schedulable unit

Date: 2026-08-07
Status: Accepted
Session: SESSION 1

## Context

Academic compatibility means several programmes' courses may be *taught
together* — one room, one teacher, one time, three curricula. The obvious
model is to let a `CourseOffering` realise an `AcademicCompatibilityGroup`
spanning several courses.

Architecture review rejected that. If an offering can name several courses,
then a student whose curriculum requires MAT150 enrolls in an offering whose
course is MAT101, and `Enrollment` records only the offering. Three things
break at once:

- the requirement the enrollment satisfied becomes unrecoverable;
- `attempt_number` and repeated-course detection break, because attempts are
  counted per *course*, not per offering;
- SESSION 5's "how many alternative offerings does this student have?" becomes
  uncomputable.

Worse, SESSION 11 lists "merge compatible offerings" as a live what-if
perturbation. Under that model a merge means rewriting `course_offering_id` on
enrollments and destroying offerings: destructive, impossible to isolate inside
a scenario, and not reversible.

Separately, `modality` was an offering-level enum. But delivery is a
per-session property — a hybrid section has in-person labs and online lectures,
and an asynchronous component has no time and no room at all. With modality
only on the offering, `ScheduleAssignment.room_id IS NULL` cannot distinguish
"virtual class" from "room not assigned yet", which makes "is this version
complete enough to publish?" undecidable.

## Decision

1. **A `CourseOffering` always names exactly one `Course`.**
2. Compatibility is realised as **co-teaching between sessions**:
   `SessionCoTeachingGroup`, with `ClassSession.co_teaching_group_id`. Members
   must resolve to the same start timeslot and room, and room capacity uses the
   summed demand of the member sections. The group must be backed by an
   APPROVED `AcademicCompatibilityGroup`.
3. `Enrollment` carries `course_id`, bound to its offering by the composite
   foreign key `(course_offering_id, course_id) → course_offering (id,
   course_id)`.
4. `ClassSession` is **the schedulable unit** and carries explicit
   `requires_timeslot` and `requires_room` booleans plus its own
   `delivery_mode`. Offering-level `modality` and `campus` remain as the
   *envelope* (they determine enrollment eligibility and the SESSION 5
   alternative-offering count).
5. Sessions with `requires_timeslot = false` generate no decision variable and
   do not block publication.

## Consequences

- Merging and unmerging is inserting and deleting link rows. No enrollment is
  rewritten, no offering destroyed, and a scenario can propose a merge without
  touching committed data.
- SESSION 3's CP-SAT builder needs a group-aware room-capacity path from the
  start. Retrofitting co-assignment into a model that assumes session
  independence would be a rewrite, which is why the table exists now even
  though SESSION 11 is what will use it.
- ScheduleDiff must treat a co-teaching group as moving atomically.
- Publication completeness is checkable, and every scheduled session has a
  defined campus for the travel-time constraint.

## Alternatives considered

- **Offering realises the group; enrollments migrate.** Rejected — see above.
- **N:M between `Section` and `ClassSession`.** Rejected: it makes the
  schedulable unit ambiguous, which is the one thing SESSION 2 cannot tolerate.
- **Modality on `Section`.** Rejected: a single section legitimately mixes
  in-person and online sessions.

## Tests

`backend/tests/test_offering_hierarchy.py` — offering names one course;
duplicate offerings rejected; asynchronous sessions need no room; a session
requiring a room must require a timeslot; a section may mix session types.
`backend/tests/test_schedule.py::TestRoomOccupancy` — `requires_room` must
match the session; an asynchronous session takes no room.
