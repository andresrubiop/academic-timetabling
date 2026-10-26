# ADR-0005: Enrollment as the immutable academic record of intent

Date: 2026-08-07
Status: Accepted
Session: SESSION 1

## Context

"Enrollment is authoritative for student conflicts" (playbook SESSION 1).
Everything the optimizer knows about which students collide comes from this
table.

An enrollment must stay interpretable for years. Curricula are versioned and
revised; a student may repeat a course three periods after first taking it.
An enrollment that merely pointed at "the course" and "the curriculum" becomes
unreadable the moment either changes — you can no longer answer *which
requirement this enrollment satisfied*, which is exactly what SESSION 5's
mandatory-status and progression-risk inputs need.

Architecture review also found that the first design allowed split-brain
states the database would happily accept: an enrollment in period 2026-1
pointing at a 2025-2 offering, or at a section belonging to a different
offering entirely. The same class of drift existed between section and
offering, assignment and timeslot, and availability and period.

This is the one category of error that cannot be repaired later. You cannot
backfill which curriculum requirement a two-year-old enrollment satisfied.

## Decision

1. `Enrollment` snapshots its academic meaning:
   `course_id`, `academic_period_id`, `curriculum_version_id`,
   `curriculum_course_id`, `is_mandatory`, `enrollment_kind`,
   `attempt_number`.
2. Composite foreign keys bind every denormalised copy to its source:
   - `(course_offering_id, course_id) → course_offering (id, course_id)`
   - `(course_offering_id, academic_period_id) → course_offering (id,
     academic_period_id)`
   - `(section_id, course_offering_id) → section (id, course_offering_id)`
3. `CHECK (NOT is_mandatory OR curriculum_course_id IS NOT NULL)` — a
   mandatory enrollment must name the requirement it satisfies.
4. `CHECK (attempt_number = 1 OR enrollment_kind = 'REPEATED')`.
5. The snapshot is **immutable once the enrollment leaves `REQUESTED`**,
   enforced by the `enrollment_snapshot_is_immutable` trigger. `status` and
   `section_id` remain editable: withdrawal and sectioning are ordinary
   registrar operations.
6. The **composite-FK pattern is applied schema-wide** wherever a period,
   course or campus is denormalised (section, class_session, schedule_version,
   schedule_assignment, room, student_cohort_membership).

## Consequences

- Enrollment rows are wider, and writing one requires resolving the curriculum
  requirement at enrollment time. That is correct: it *is* the academic act.
- SESSION 15's imports must resolve the curriculum row or fail loudly, which
  the playbook already demands ("never silently import malformed rows").
- Cross-period and cross-offering corruption is structurally impossible rather
  than caught by review.

## Alternatives considered

- **Derive everything through `CurriculumCourse` at read time.** Rejected:
  breaks under curriculum versioning, which is the whole problem.
- **A bare denormalised `is_mandatory` boolean.** Rejected: it records the
  conclusion but discards the evidence, so progression risk stays
  uncomputable.

## Tests

`backend/tests/test_enrollment.py` — snapshot binding (course, period, section
must match the offering), academic meaning (mandatory requires a curriculum
requirement, repeats are marked), and immutability (frozen after confirmation,
correctable while REQUESTED, status/section still editable).
