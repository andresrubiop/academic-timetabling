"""CP-SAT implementation of the SESSION 2 model (SESSION 3).

This is the only layer that imports OR-Tools. The model it solves lives in
`academic_ai.domain.scheduling` and stays solver-agnostic, so the problem can
be argued about without a solver and a different solver could be tried without
redefining the problem.

* `variables` — CP-SAT variables, built from the domains the model computed.
* `adapters`  — one adapter per catalog code, registered by name.
* `objective` — the lexicographic driver.
* `validator` — independent re-derivation of every HARD check.
* `solve`     — orchestration, and the refusal to solve what should not be.
"""

from academic_ai.optimization.adapters import (
    REGISTRY,
    AdapterResult,
    apply_hard_constraints,
    registered_codes,
)
from academic_ai.optimization.objective import TierOutcome, optimise_lexicographically
from academic_ai.optimization.solve import SolveOutcome, SolverConfig, solve
from academic_ai.optimization.validator import (
    Assignment,
    ValidationReport,
    Violation,
    validate,
)
from academic_ai.optimization.variables import ModelVariables, build_variables

__all__ = [
    "REGISTRY",
    "AdapterResult",
    "Assignment",
    "ModelVariables",
    "SolveOutcome",
    "SolverConfig",
    "TierOutcome",
    "ValidationReport",
    "Violation",
    "apply_hard_constraints",
    "build_variables",
    "optimise_lexicographically",
    "registered_codes",
    "solve",
    "validate",
]
