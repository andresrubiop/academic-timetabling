"""Structured JSON logs that do not leak the thing they are describing.

One JSON object per line, because these logs are read by `docker logs` and by
whatever aggregator SESSION 15 chooses, and a human-friendly multi-line format
is unparseable by both.

The redaction is the point
==========================

An auth layer logs exactly the objects that must never appear in a log:
request bodies with passwords, headers with bearer tokens, settings with the
signing key. `redact()` walks a structure and replaces the *values* of
sensitive-looking keys, and truncates anything that looks like a JWT wherever
it appears — including inside a message string, where a well-meaning
`f"failed for {authorization}"` would otherwise put a live token on disk.

Redaction by key name is not a security boundary; it is a seatbelt. The
boundary is not passing secrets to the logger, and `SENSITIVE_KEYS` catches
the times somebody does anyway.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from typing import Any, Mapping

__all__ = [
    "REDACTED",
    "SAFE_KEYS",
    "SENSITIVE_KEYS",
    "JsonFormatter",
    "configure_logging",
    "redact",
]

REDACTED = "[redacted]"

#: Keys that *look* sensitive by substring but carry a derived, non-secret
#: value. `SecuritySettings.redacted()` deliberately reports the signing key's
#: **length** -- enough to diagnose "the key got truncated", useless to an
#: attacker -- and without this list the redactor would hide the very thing
#: that method exists to expose. Two safety mechanisms silently cancelling
#: each other out is worse than either one alone, because the log still looks
#: like it is working.
#:
#: Exact matches only, deliberately. A substring rule here would be a hole:
#: `secret_key_length_and_also_the_key` would match.
SAFE_KEYS = frozenset(
    {
        "secret_key_length",
        "access_token_minutes",
        "refresh_token_days",
        "token_type",
        "password_changed_at",
        "last_login_at",
        "failed_attempts",
        "must_change_password",
        "session_id",
        "has_artifact",
    }
)

#: Matched case-insensitively as a *substring*, so `POSTGRES_PASSWORD`,
#: `password_hash` and `refreshToken` are all caught without enumerating
#: every spelling anybody will ever invent.
SENSITIVE_KEYS = (
    "password",
    "secret",
    "token",
    "authorization",
    "api_key",
    "apikey",
    "cookie",
    "session",
    "credential",
    "private",
    "salt",
    "hash",
)

#: `header.payload.signature` in base64url. Deliberately loose: a false
#: positive redacts something harmless, a false negative writes a live
#: credential to disk.
_JWT = re.compile(r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

#: Bearer/Basic values, which are not always JWTs.
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    if lowered in SAFE_KEYS:
        return False
    return any(marker in lowered for marker in SENSITIVE_KEYS)


def _scrub_text(value: str) -> str:
    value = _AUTH_HEADER.sub(lambda m: f"{m.group(1)} {REDACTED}", value)
    return _JWT.sub(REDACTED, value)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Copy a structure with sensitive values replaced.

    Depth-limited: a cyclic or absurdly nested object must not turn a log call
    into a stack overflow inside the logging path, where the error has nowhere
    to be reported.
    """
    if _depth > 6:
        return "[truncated]"
    if isinstance(value, Mapping):
        return {
            key: REDACTED if _is_sensitive(str(key)) else redact(val, _depth=_depth + 1)
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value]
    if isinstance(value, str):
        return _scrub_text(value)
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line, with the extras redacted."""

    _SKIP = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
        "message", "asctime", "taskName",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": dt.datetime.fromtimestamp(
                record.created, dt.timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _scrub_text(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key not in self._SKIP and not key.startswith("_"):
                payload[key] = REDACTED if _is_sensitive(key) else redact(value)
        if record.exc_info:
            payload["exception"] = _scrub_text(self.formatException(record.exc_info))
        # `default=str` so an unserialisable extra degrades to its repr rather
        # than raising inside the logging call and losing the whole record.
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", *, stream: Any = None) -> None:
    """Install the JSON formatter as the only root handler.

    Replaces existing handlers rather than adding to them: uvicorn and celery
    both install their own, and leaving those in place emits every line twice
    — once structured and once not, with the unstructured copy unredacted.
    """
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
