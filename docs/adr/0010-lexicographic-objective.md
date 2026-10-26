# ADR-0010: Lexicographic objective and the catalog as the floor of HARD enforcement

Date: 2026-08-14
Status: Accepted
Session: SESSION 2

## Context

SESSION 2 formalises the timetabling problem. Two decisions in it are
hard to reverse once SESSION 3 builds a solver against them.

**The objective.** The obvious design is one weighted sum with stratified
weights. It fails twice: separation requires each weight to exceed the total
of everything below it, which at five tiers and institutional magnitudes lands
around 10^15–10^20 — past int64 and past the point where CP-SAT's bound
pruning helps; and the numbers would be invented, which the playbook forbids
in exactly those words.

**The enforced constraint set.** The first design derived it from the
`planning_constraint` rows that happened to exist. Independent review by
`optimizer-agent` found that an instance configuring nothing therefore
enforced nothing, while `missing_adapters()` reported a complete adapter
registry — a solver returning OPTIMAL for a timetable that double-books a
teacher. Separately, `is_active = false` switched off even a non-overridable
HARD constraint, with no approver, no reason and no audit trail, bypassing
both the category system and the database's `override_requires_authority`
CHECK.

## Decision

1. **The objective is lexicographic**, solved one tier at a time: minimise
   `f_k`, post `f_k ≤ v_k + tolerance_k`, continue. `tolerance` defaults to
   zero; a non-zero value is an institutional decision that must be recorded.
2. **Two profiles ship**, GREENFIELD and MINIMAL_DISRUPTION, differing only in
   where schedule stability sits. Stability never outranks mandatory student
   conflicts.
3. **Terms may be summed inside a tier only if they share both a `TermUnit`
   and a `ViolationMeasure`**, or the tier counts `INDICATOR` — dimensionless
   violated-instance counts, where the human weight is the only exchange rate.
4. **Weights are scaled by exactly `WEIGHT_SCALE = 1000`.** Both weight
   columns are `Numeric(_,3)`, so this is lossless rather than a rounding
   policy. No second scaling factor may exist anywhere in the stack.
5. **The catalog is the floor of HARD enforcement.** `hard_codes()` is derived
   from `CONSTRAINT_CATALOG`, not from configured rows: an absent row means
   "no instance-specific parameters", never "switched off".
6. **`is_active = false` is rejected for a non-overridable HARD constraint.**
   Relaxing physical reality goes through the override path, with a named
   approver and a written reason, or it does not happen.

## Consequences

- SESSION 3 runs one CP-SAT solve per tier and must record each tier's
  optimum, status and bound as an `OptimizationMetric`. A tier that returns
  FEASIBLE rather than OPTIMAL makes the lexicographic claim false and must
  say so.
- Adding a constraint to the catalog automatically adds it to the enforced
  floor, so `missing_adapters()` will demand an adapter for it. That is the
  intended direction of failure.
- An institution that disagrees with a tier order changes the profile, and the
  profile is recorded on the run, so past results stay interpretable.

## Alternatives considered

- **Weighted sum with stratified weights.** Rejected — see Context.
- **Deriving the enforced set from configuration.** Rejected: it makes silent
  non-enforcement the default failure mode.
- **A single relaxed-hard tier.** Rejected: it would sum a stranded student
  against a room two seats over capacity at an implicit 1:1 rate.

## Tests

`backend/tests/test_objective.py` — tier order, no double counting, unit and
measure commensurability, tolerance arithmetic.
`backend/tests/test_scheduling_model.py::TestProblemInstance` — the catalog
floor, and rejection of deactivating physical reality.
`backend/tests/test_constraint_catalog.py` — classification coherence and the
seeded `constraint_type` table matching the catalog.
