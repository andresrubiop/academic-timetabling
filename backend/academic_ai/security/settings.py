"""Configuration, and the refusal to start without it.

Every value comes from the environment. The interesting behaviour is not
reading them — it is **refusing to boot** when a secret is missing, too short,
or still carries the placeholder from `.env.example`.

Why fail closed, loudly, at import of the app rather than at first use
=====================================================================

A missing `BACKEND_SECRET_KEY` that defaults to ``""`` produces a service
that starts, serves, signs every token with an empty key and accepts every
forgery. Nothing in the logs looks wrong. The failure surfaces weeks later as
an incident nobody can date.

A placeholder secret is worse than a missing one, because it is *published*:
`.env.example` is in the repository, so `replace-with-a-unique-value-...` is
a known key to anyone who can read GitHub. `_PLACEHOLDER_MARKERS` exists so
that copying the template and forgetting to edit it is an error rather than a
deployment.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from typing import Mapping, Sequence

__all__ = [
    "MIN_SECRET_BYTES",
    "ConfigurationError",
    "SecuritySettings",
    "generate_secret",
    "load_security_settings",
]

#: 32 bytes of entropy, the usual HMAC-SHA256 key size. Below this the signing
#: key is the weakest part of the token.
MIN_SECRET_BYTES = 32

#: Substrings that mean "this came from the template and was never edited".
_PLACEHOLDER_MARKERS = (
    "replace-with",
    "change-me",
    "changeme",
    "your-secret",
    "placeholder",
    "example",
    "dev_only",
    "not-a-secret",
    "not_a_secret",
)


class ConfigurationError(RuntimeError):
    """The process must not continue. Never contains the offending value."""


def generate_secret(nbytes: int = MIN_SECRET_BYTES) -> str:
    """What the deployment docs tell an operator to run. Here so the docs and
    the validator cannot disagree about what "long enough" means."""
    return secrets.token_hex(nbytes)


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigurationError(
            f"{name} is not set. Generate one with "
            f"`python -c 'import secrets; print(secrets.token_hex(32))'` and "
            f"put it in .env (which is gitignored and must stay that way)."
        )
    return value


def _require_secret(env: Mapping[str, str], name: str) -> str:
    value = _require(env, name)
    lowered = value.lower()
    for marker in _PLACEHOLDER_MARKERS:
        if marker in lowered:
            raise ConfigurationError(
                f"{name} still contains the placeholder text from "
                f".env.example ({marker!r}). That value is committed to this "
                "repository, so it is public. Generate a real one."
            )
    if len(value) < MIN_SECRET_BYTES:
        raise ConfigurationError(
            f"{name} is shorter than {MIN_SECRET_BYTES} characters. A signing "
            "key shorter than the digest it produces is the weakest part of "
            "the token."
        )
    return value


def _split(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


@dataclass(frozen=True, slots=True)
class SecuritySettings:
    """Everything the auth layer needs, already validated."""

    secret_key: str
    environment: str
    allowed_hosts: tuple[str, ...]
    cors_origins: tuple[str, ...]
    access_token_minutes: int = 30
    refresh_token_days: int = 7
    database_url: str = ""
    redis_url: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}

    def redacted(self) -> Mapping[str, object]:
        """Safe to log at startup. The secret is reported by *length* only —
        enough to diagnose "the key got truncated", useless to an attacker."""
        return {
            "environment": self.environment,
            "secret_key_length": len(self.secret_key),
            "allowed_hosts": list(self.allowed_hosts),
            "cors_origins": list(self.cors_origins),
            "access_token_minutes": self.access_token_minutes,
            "refresh_token_days": self.refresh_token_days,
            "database_configured": bool(self.database_url),
            "redis_configured": bool(self.redis_url),
        }


def load_security_settings(
    env: Mapping[str, str] | None = None,
) -> SecuritySettings:
    """Read and validate. Raises `ConfigurationError` rather than defaulting.

    ``env`` is a parameter so this is testable without mutating the real
    process environment — a test that has to `os.environ[...] = ...` leaks
    into every test after it.
    """
    env = os.environ if env is None else env

    settings = SecuritySettings(
        secret_key=_require_secret(env, "BACKEND_SECRET_KEY"),
        environment=env.get("ENVIRONMENT", "development").strip() or "development",
        allowed_hosts=_split(env.get("BACKEND_ALLOWED_HOSTS", "")),
        cors_origins=_split(env.get("BACKEND_CORS_ORIGINS", "")),
        access_token_minutes=int(env.get("ACCESS_TOKEN_EXPIRE_MINUTES", "30")),
        refresh_token_days=int(env.get("REFRESH_TOKEN_EXPIRE_DAYS", "7")),
        database_url=env.get("DATABASE_URL", "").strip(),
        redis_url=env.get("REDIS_URL", "").strip(),
    )

    if settings.is_production:
        # Checks that are merely annoying in development and are the whole
        # point in production.
        if not settings.allowed_hosts:
            raise ConfigurationError(
                "BACKEND_ALLOWED_HOSTS is empty in production. An empty "
                "allow-list means every Host header is accepted, which makes "
                "password-reset links forgeable."
            )
        if "*" in settings.cors_origins:
            raise ConfigurationError(
                "BACKEND_CORS_ORIGINS contains '*' in production. With "
                "credentialed requests that is every site on the internet "
                "reading this institution's timetables."
            )
        if settings.access_token_minutes > 60:
            raise ConfigurationError(
                "ACCESS_TOKEN_EXPIRE_MINUTES is over 60 in production. Access "
                "tokens cannot be revoked; that is why they are short."
            )
        if not settings.database_url:
            raise ConfigurationError("DATABASE_URL is not set in production")
    return settings
