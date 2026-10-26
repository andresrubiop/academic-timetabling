"""The mathematical model of the university timetabling problem (SESSION 2).

Solver-agnostic on purpose. Nothing here imports OR-Tools: this package states
*what* the problem is, and SESSION 3 states how CP-SAT is asked to solve it.
Keeping the two apart is what lets the model be reviewed, tested and argued
about without a solver in the room — and what lets a second solver be tried
later without redefining the problem.

Reading order:

* `variables` — the two decision variables, and the list of things that are
  deliberately *not* decision variables.
* `catalog` — every constraint the platform can state, classified
  HARD/MEDIUM/SOFT with a written rationale for each.
* `objective` — lexicographic tiers, and why not a weighted sum.
* `problem` — the instance, and the `(base data, overlay)` builder contract
  required by ADR-0007.
* `diagnostics` — the named ways an instance can be wrong or misleading.
* `fixtures` — small deterministic instances, each isolating one phenomenon.
"""

from academic_ai.domain.scheduling.catalog import (
    CONSTRAINT_CATALOG,
    PLAYBOOK_COVERAGE,
    ConstraintSpec,
    downgradable_hard_codes,
    objective_bearing_codes,
    solver_hard_codes,
)
from academic_ai.domain.scheduling.diagnostics import (
    DIAGNOSTIC_SEVERITY,
    Diagnostic,
    DiagnosticCode,
)
from academic_ai.domain.scheduling.enums import (
    DecisionVariable,
    DiagnosticSeverity,
    Enforcement,
    TermUnit,
    VariableDependency,
    WeightSource,
)
from academic_ai.domain.scheduling.objective import (
    OBJECTIVE_PROFILES,
    GREENFIELD,
    MINIMAL_DISRUPTION,
    ObjectiveProfile,
    ObjectiveTier,
    TierMembership,
    Tolerance,
)
from academic_ai.domain.scheduling.priority import (
    MAX_PRIORITY,
    MIN_PRIORITY,
    ConflictPriorityProvider,
    HeuristicConflictPriority,
    PriorityRequest,
    conflict_pair_weights,
    criticality,
    resolve_priorities,
)
from academic_ai.domain.scheduling.problem import (
    BaseAssignment,
    BaseSnapshot,
    CoTeachingGroupSpec,
    EffectiveConstraint,
    Overlay,
    Perturbation,
    PeriodSpec,
    ProblemBuilder,
    ProblemInstance,
    RoomSpec,
    SessionSpec,
    SlotSpec,
    StudentEnrollmentSpec,
    TeacherSpec,
    classify_student_conflict,
    missing_adapters,
)
from academic_ai.domain.scheduling.variables import (
    DECISION_VARIABLES,
    NON_DECISION_INPUTS,
    DaySlot,
    feasible_start_indices,
    occupied_in_day_indices,
    resolve_effective_section,
)

__all__ = [
    "CONSTRAINT_CATALOG",
    "DECISION_VARIABLES",
    "DIAGNOSTIC_SEVERITY",
    "GREENFIELD",
    "MINIMAL_DISRUPTION",
    "NON_DECISION_INPUTS",
    "OBJECTIVE_PROFILES",
    "PLAYBOOK_COVERAGE",
    "BaseAssignment",
    "BaseSnapshot",
    "CoTeachingGroupSpec",
    "ConstraintSpec",
    "DaySlot",
    "DecisionVariable",
    "Diagnostic",
    "DiagnosticCode",
    "DiagnosticSeverity",
    "EffectiveConstraint",
    "Enforcement",
    "ObjectiveProfile",
    "ObjectiveTier",
    "Overlay",
    "PeriodSpec",
    "Perturbation",
    "ProblemBuilder",
    "ProblemInstance",
    "RoomSpec",
    "SessionSpec",
    "SlotSpec",
    "StudentEnrollmentSpec",
    "TeacherSpec",
    "TermUnit",
    "TierMembership",
    "Tolerance",
    "VariableDependency",
    "WeightSource",
    "MAX_PRIORITY",
    "MIN_PRIORITY",
    "ConflictPriorityProvider",
    "HeuristicConflictPriority",
    "PriorityRequest",
    "conflict_pair_weights",
    "criticality",
    "resolve_priorities",
    "classify_student_conflict",
    "downgradable_hard_codes",
    "feasible_start_indices",
    "missing_adapters",
    "objective_bearing_codes",
    "occupied_in_day_indices",
    "resolve_effective_section",
    "solver_hard_codes",
]
