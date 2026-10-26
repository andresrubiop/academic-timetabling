# ADR-0009: Sectioning authority and the student-overlap precondition

Date: 2026-08-07
Status: Accepted
Session: SESSION 1

## Context

`Enrollment.section_id` is nullable: academic intent exists before a student
is placed in a section. The first design also said "the optimizer may choose
sectioning in later sessions". Architecture review pointed out those cannot
both hold.

If the optimizer chooses sectioning, the placement varies per
`ScheduleVersion` and per `Scenario`. Writing it back to `Enrollment` would
overwrite the registrar's record with a proposal — the same category of error
as storing assignments on `ClassSession`.

The schema also admits a state CP-SAT cannot express: with `section_id` NULL,
the student-overlap constraint has no section to bind to. The optimizer must
either enumerate a disjunction over every section (combinatorial blow-up) or
silently drop the student (wrong, and invisible).

Review further found that SESSION 5's alternatives count was not correctly
computable: "available alternative section" requires remaining capacity =
`Section.capacity − count(active enrollments in that section)`, but
**unsectioned enrollments are invisible to that count**. Remaining capacity is
therefore systematically overestimated, which systematically *understates* the
criticality of the student with a repeated mandatory course and one section —
the exact inversion of the rule SESSION 5 exists to enforce.

## Decision

1. **`Enrollment.section_id` is registrar-authoritative and nullable.**
   Sectioning is **not** an optimizer decision variable in SESSIONS 2–5.
2. If sectioning ever becomes an optimizer decision, it must be version-scoped
   in its own entity and must **never** write to `Enrollment`.
3. A `ScheduleVersion` may only be optimized or published when every
   non-`DROPPED`, non-`REQUESTED` enrollment in scope has `section_id` set.
   SESSION 3's pre-solve validator enforces this; SESSION 6 reports it as an
   ERROR with a named diagnostic, never a silent skip.
4. Remaining capacity must count **both** placed and unplaced active demand.
   A partial index on `enrollment (section_id) WHERE status IN ('REQUESTED',
   'ENROLLED', 'WAITLISTED')` serves the hot path.
5. "Alternative sections" is a **static** count — active, not full, eligible.
   Schedule-dependent alternatives are a SESSION 5 concern computed against a
   specific version.
6. Students are attached to a programme and a curriculum version directly
   (`Student.program_id`, `Student.curriculum_version_id`), so eligibility and
   progression risk are answerable without going through cohorts. A
   `StudentCohort` is a planning and reporting convenience and is **never** a
   conflict authority — two students in one cohort routinely differ once one
   is repeating a course.

## Consequences

- SESSION 5 becomes computable, and the "one repeated mandatory student
  outranks many with alternatives" rule becomes demonstrable rather than
  aspirational.
- Unsectioned enrollments become a loud, diagnosable precondition failure
  instead of a silent omission from the model.

## Tests

`backend/tests/test_student_criticality_inputs.py` — alternatives are
countable; a full or cancelled section is not an alternative; **unplaced
enrollments are real demand**; only APPROVED equivalences count; and the
playbook's headline case (one repeated mandatory student with one section
versus twenty with alternatives) is identifiable from the schema alone.
