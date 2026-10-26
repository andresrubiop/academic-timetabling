# ADR-0008: Teacher assignment is authoritative at session level

Date: 2026-08-07
Status: Accepted
Session: SESSION 1

## Context

Universities think about teaching at the section level: "Professor X teaches
section A". So the natural model is `SectionTeacher` as the authority, with a
nullable per-session override for the case where the lab has a different
instructor.

Architecture review rejected that, decisively. The resolution rule — "use the
session's teacher if set, otherwise the section's PRIMARY teachers" — is a
function that would have to be reimplemented *identically* in the CP-SAT
builder, the independent validator, the LNS repair loop, the ScheduleDiff
engine and the API. No database constraint can detect a divergence between
those implementations, and the failure mode is a schedule that double-books a
human being.

"The section's PRIMARY teachers" is also already plural, and therefore already
ambiguous.

Teacher workload, daily limits and teacher gaps — all SESSION 2 requirements —
are per-session quantities regardless of where the assignment is recorded.

## Decision

- **`SessionTeacher` is the sole scheduling authority.** Teacher overlap is a
  direct group-by over `session_teacher ⋈ schedule_assignment`, with no
  interpretation layer.
- `SectionTeacher` is retained for contractual load and payroll-style
  reporting, and is documented in the model itself as **not authoritative for
  scheduling**.
- Whether a role occupies the teacher's time is decided by the named constant
  `TIME_OCCUPYING_TEACHER_ROLES` (`PRIMARY`, `ASSISTANT`), not by a string
  list inside the optimizer — so a `SUBSTITUTE` cannot silently enter the
  overlap constraint.

## Consequences

- The common single-teacher case costs N rows for a section with N sessions.
  This is negligible.
- Data-entry ergonomics ("assign at section level, fan out on write") belong
  in the application layer, not in a read-time resolution rule.
- Staffing completeness — "every schedulable session has at least one
  time-occupying teacher" — becomes a validation rather than an inference.

## Alternatives considered

- **Section-authoritative with a session override.** Rejected — see above.
- **Both, with one derived by trigger.** Rejected: it reintroduces the same
  resolution rule, only harder to debug.

## Tests

`backend/tests/test_offering_hierarchy.py::TestTeacherAssignment` — teaching
is recorded per session; a lab may have a different instructor; co-teaching is
two rows; a teacher is assigned to a session once; only time-occupying roles
enter the overlap constraint.
