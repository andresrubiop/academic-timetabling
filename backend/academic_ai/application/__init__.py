"""Use cases (SESSION 9).

The layer between the API and everything else. It exists so that "log a user
in" and "submit an optimization run" are testable without a web framework, and
so that HTTP concerns -- status codes, request models, headers -- never leak
into a decision about whether an account is locked.

Depends on infrastructure (it needs a database session and a queue) and on the
domain. Nothing here imports FastAPI.
"""

from academic_ai.application.auth import (
    AuthError,
    AuthService,
    LoginResult,
    LockedOut,
)
from academic_ai.application.optimization import (
    OptimizationService,
    RunNotFound,
    RunSubmission,
)

__all__ = [
    "AuthError",
    "AuthService",
    "LockedOut",
    "LoginResult",
    "OptimizationService",
    "RunNotFound",
    "RunSubmission",
]
