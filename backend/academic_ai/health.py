"""Container healthcheck. ``python -m academic_ai.health [--worker]``.

A healthcheck that always passes is worse than none: compose believes the
service is up, `depends_on: service_healthy` lets the next one start, and the
first symptom is a 502 from nginx.

So this checks the things whose absence actually makes the process useless —
configuration, database, Redis — and exits non-zero when any of them fails.
It deliberately does **not** check anything expensive: it runs every 15
seconds forever, and a healthcheck that runs a query against a large table is
a self-inflicted load problem.

Prints one JSON object, matching the structured logs, so a failing check in
`docker inspect` output is readable rather than a bare exit code.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


def _check_settings() -> tuple[bool, str]:
    from academic_ai.security.settings import ConfigurationError, load_security_settings

    try:
        load_security_settings()
    except ConfigurationError as exc:
        # The message never contains the value, only which variable.
        return False, str(exc).split(".")[0]
    return True, "ok"


def _check_database() -> tuple[bool, str]:
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return False, "DATABASE_URL is not set"
    try:
        from sqlalchemy import create_engine, text

        # A tiny pool with a short timeout: this must fail fast rather than
        # hang until the healthcheck's own timeout kills it, which reports as
        # "unhealthy" with no reason attached.
        engine = create_engine(url, pool_pre_ping=True, connect_args={"connect_timeout": 3})
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
        finally:
            engine.dispose()
    except Exception as exc:  # noqa: BLE001 - any failure is unhealthy
        return False, type(exc).__name__
    return True, "ok"


def _check_redis() -> tuple[bool, str]:
    url = os.environ.get("REDIS_URL", "").strip()
    if not url:
        return False, "REDIS_URL is not set"
    try:
        import redis  # type: ignore[import-not-found]
    except ModuleNotFoundError:
        # SESSION 9 adds the queue. Until then Redis is provisioned but not
        # spoken to, and reporting unhealthy for a dependency nothing uses
        # would keep the stack from ever starting.
        return True, "skipped (client not installed)"
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=3, socket_timeout=3)
        client.ping()
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__
    return True, "ok"


def _check_optimizer() -> tuple[bool, str]:
    """Worker only: can this process actually build a model?

    The failure this catches is a real one — `ortools` went undeclared in
    `pyproject.toml` from SESSION 3 to SESSION 8, so an image built from it
    would have started, reported healthy, accepted jobs and failed every one
    of them on import.
    """
    try:
        from academic_ai.optimization.variables import build_variables  # noqa: F401
        from ortools.sat.python import cp_model

        cp_model.CpModel()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"[:200]
    return True, "ok"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    is_worker = "--worker" in argv

    checks: dict[str, tuple[bool, str]] = {
        "settings": _check_settings(),
        "database": _check_database(),
        "redis": _check_redis(),
    }
    if is_worker:
        checks["optimizer"] = _check_optimizer()

    healthy = all(ok for ok, _ in checks.values())
    report: dict[str, Any] = {
        "status": "ok" if healthy else "unhealthy",
        "role": "worker" if is_worker else "api",
        "checks": {name: {"ok": ok, "detail": detail} for name, (ok, detail) in checks.items()},
    }
    print(json.dumps(report))
    return 0 if healthy else 1


if __name__ == "__main__":
    raise SystemExit(main())
