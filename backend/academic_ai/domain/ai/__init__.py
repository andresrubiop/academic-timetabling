"""The AI model registry, as a domain (SESSION 7).

Framework-free like the rest of `domain/`: the lifecycle rules here have to be
checkable by a remote optimizer worker holding no database connection, and by
a test that never starts Postgres. The schema in
`infrastructure/db/models/ai_registry.py` enforces the same rules again with
constraints and indexes, deliberately — the two are a belt and a pair of
braces, and the schema is the one that survives a bug in this file.
"""

from academic_ai.domain.ai.lifecycle import (
    ACTION_TRANSITIONS,
    TERMINAL_STATUSES,
    LifecycleError,
    ModelAction,
    ModelVersionState,
    apply_action,
    is_serving_status,
    permitted_actions,
)
from academic_ai.domain.ai.registry import (
    CompatibilityReport,
    FeatureSchemaRef,
    ModelContract,
    ModelEvaluationRecord,
    OptimizerCompatibility,
    RuntimeEnvironment,
    check_compatibility,
)

__all__ = [
    "ACTION_TRANSITIONS",
    "TERMINAL_STATUSES",
    "CompatibilityReport",
    "FeatureSchemaRef",
    "LifecycleError",
    "ModelAction",
    "ModelContract",
    "ModelEvaluationRecord",
    "ModelVersionState",
    "OptimizerCompatibility",
    "RuntimeEnvironment",
    "apply_action",
    "check_compatibility",
    "is_serving_status",
    "permitted_actions",
]
