"""Authentication, authorisation, secrets and structured logging (SESSION 8).

Deliberately importable without FastAPI. The optimizer worker needs the same
RBAC table and the same redacting logger as the web process, and it runs in a
container with no web framework installed.
"""

from academic_ai.security.logging import (
    JsonFormatter,
    configure_logging,
    redact,
)
from academic_ai.security.passwords import (
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    hash_password,
    needs_rehash,
    verify_password,
)
from academic_ai.security.rbac import (
    RESERVED_TO_HUMANS,
    ROLE_PERMISSIONS,
    ActorKind,
    AuthorizationError,
    Permission,
    Principal,
    authorise,
    permissions_for,
)
from academic_ai.security.settings import (
    ConfigurationError,
    SecuritySettings,
    generate_secret,
    load_security_settings,
)
from academic_ai.security.tokens import (
    TokenClaims,
    TokenError,
    TokenType,
    decode_token,
    issue_access_token,
    issue_refresh_token,
)

__all__ = [
    "ActorKind",
    "AuthorizationError",
    "ConfigurationError",
    "JsonFormatter",
    "MIN_PASSWORD_LENGTH",
    "Permission",
    "PasswordPolicyError",
    "Principal",
    "RESERVED_TO_HUMANS",
    "ROLE_PERMISSIONS",
    "SecuritySettings",
    "TokenClaims",
    "TokenError",
    "TokenType",
    "authorise",
    "configure_logging",
    "decode_token",
    "generate_secret",
    "hash_password",
    "issue_access_token",
    "issue_refresh_token",
    "load_security_settings",
    "needs_rehash",
    "permissions_for",
    "redact",
    "verify_password",
]
