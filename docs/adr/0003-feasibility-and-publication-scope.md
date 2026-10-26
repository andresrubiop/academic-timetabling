# ADR-0003: Unit of feasibility and schedule publication scope

Date: 2026-08-07
Status: Accepted
Session: SESSION 1

## Context

A `Schedule` may be scoped to the institution, a faculty, or a programme, so
that each unit can plan its own timetable and so that change requests can be
routed to the right authority (SESSION 12 distinguishes within-programme,
cross-programme and cross-faculty changes).

The first design made "at most one PUBLISHED version" a per-`Schedule` rule.
Architecture review found that this permits two PUBLISHED versions in the same
academic period — say Engineering's and Science's — that each independently
assign room R at slot 42, or book the same teacher twice. Neither violates its
own constraint set, and CP-SAT never sees the conflict because it is only ever
handed one schedule's scope.

This is not a hypothetical. The core premise of the platform is that offerings,
rooms, teachers and students are shared across programmes and faculties, so a
shared offering is in scope for two schedules at once by design. The failure is
discovered in production, by a room with two classes in it.

## Decision

Feasibility and publication are **institution-and-period-wide**.

- At most one PUBLISHED `ScheduleVersion` per `(institution_id,
  academic_period_id)`, enforced by a partial unique index.
- `Schedule.scope` is retained, but it means *authorship, filtering and
  change-request routing* — not an independent feasibility universe.
- Faculty- and programme-scoped DRAFT versions remain fully supported;
  only publication is centralised.

## Consequences

- Publishing becomes an institution-level act. This pushes SESSION 12's
  approval chain toward institutional authority for the final step, which is
  where the playbook already puts cross-faculty decisions.
- A faculty can still plan independently; it simply cannot unilaterally
  declare its plan to be *the* timetable.
- If genuinely independent per-scope publication is ever required, it needs
  explicit resource reservation (a scope claiming room/teacher time). That
  would be a new entity, not a rewrite of this one — an acceptable deferral.

## Alternatives considered

- **Independently publishable scoped schedules with resource reservation.**
  A real distributed-planning architecture. Rejected for SESSION 1 as far more
  machinery than the problem currently justifies.
- **Hard-partitioning offerings so none is in two scopes.** Rejected: it
  contradicts shared courses, common cores and cross-faculty service courses,
  which are the product.

## Tests

`backend/tests/test_schedule.py::TestPublicationScope` — two faculty-scoped
versions cannot both be published in one period; drafts alongside a published
version are fine; publishing requires a publisher and a timestamp.
