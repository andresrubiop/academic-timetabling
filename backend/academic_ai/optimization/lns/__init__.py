"""Large Neighborhood Search (SESSION 4).

`destroy` holds the eight heuristic `NeighborhoodPolicy` implementations;
`engine` runs the destroy/repair loop. The policy contract itself lives in
`academic_ai.domain.scheduling.neighborhood`, framework-free, so a learned
policy can implement it without OR-Tools in scope.
"""

from academic_ai.optimization.lns.destroy import (
    DEFAULT_POLICIES,
    POLICY_REGISTRY,
    ConflictComponentDestroy,
    HighConflictDestroy,
    ProgramDestroy,
    RandomDestroy,
    RoomBottleneckDestroy,
    StudentCriticalityDestroy,
    TeacherBottleneckDestroy,
    TimeWindowDestroy,
    policy_by_code,
)
from academic_ai.optimization.lns.engine import (
    IterationRecord,
    LnsConfig,
    LnsOutcome,
    optimise,
)

__all__ = [
    "DEFAULT_POLICIES",
    "POLICY_REGISTRY",
    "ConflictComponentDestroy",
    "HighConflictDestroy",
    "IterationRecord",
    "LnsConfig",
    "LnsOutcome",
    "ProgramDestroy",
    "RandomDestroy",
    "RoomBottleneckDestroy",
    "StudentCriticalityDestroy",
    "TeacherBottleneckDestroy",
    "TimeWindowDestroy",
    "optimise",
    "policy_by_code",
]
