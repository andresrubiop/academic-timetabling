"""The FastAPI application.

Assembled here rather than at import of a module-level `app`, so a test can
build one against its own settings and database without the process-wide
singletons the deployed app uses.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import logging
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from academic_ai.api.routers import (
    academic,
    availability,
    auth,
    change_requests,
    dashboards,
    governance,
    health,
    imports,
    notifications,
    optimization,
    schedule,
    scopes,
    sessions as session_routes,
    users,
)
from academic_ai.security.logging import configure_logging
from academic_ai.security.settings import SecuritySettings, load_security_settings

__all__ = ["create_app"]

log = logging.getLogger("academic_ai.api")

API_PREFIX = "/api/v1"


def create_app(settings: SecuritySettings | None = None) -> FastAPI:
    configure_logging(os.environ.get("LOG_LEVEL", "INFO"))
    # Loaded before anything is mounted, so a deployment with a missing or
    # placeholder secret fails at startup rather than on the first login.
    settings = settings or load_security_settings()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Startup. The settings were already validated above; logging them
        # redacted here gives an operator one line that proves which
        # configuration the process actually came up with.
        log.info("api starting", extra={"settings": settings.redacted()})
        yield
        log.info("api stopping")

    app = FastAPI(
        title="Academic AI Planning Platform",
        lifespan=lifespan,
        version="0.1.0",
        # Interactive docs off in production: they publish every route,
        # schema and example to anybody who finds the host.
        docs_url=None if settings.is_production else "/docs",
        redoc_url=None,
        openapi_url=None if settings.is_production else "/openapi.json",
    )
    app.state.settings = settings

    if settings.allowed_hosts:
        # Rejects a forged Host header, which is what makes an absolute URL in
        # a password-reset email trustworthy.
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PATCH", "DELETE"],
            allow_headers=["Authorization", "Content-Type"],
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        """One structured log line per request, with a correlation id.

        The id comes from nginx when nginx set one, so a line here and a line
        in the proxy log can be joined. Never logs the body or the
        Authorization header -- the redacting formatter would catch a token
        that slipped through, but not logging it is the actual control.
        """
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        started = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            log.exception(
                "request failed",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                },
            )
            raise
        elapsed_ms = round((time.monotonic() - started) * 1000, 2)
        response.headers["X-Request-Id"] = request_id
        log.info(
            "request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": elapsed_ms,
            },
        )
        return response

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Never leak a traceback or an exception message to a client.

        A stack trace in a 500 body names file paths, library versions and
        sometimes query fragments. The detail is in the log, correlated by
        request id, where the operator can read it and the caller cannot.
        """
        log.exception(
            "unhandled exception",
            extra={"path": request.url.path, "method": request.method},
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "internal error", "request_id": request.headers.get("x-request-id")},
        )

    app.include_router(health.router)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(optimization.router, prefix=API_PREFIX)
    app.include_router(schedule.router, prefix=API_PREFIX)
    app.include_router(governance.router, prefix=API_PREFIX)
    app.include_router(academic.router, prefix=API_PREFIX)
    app.include_router(availability.router, prefix=API_PREFIX)
    # Before `academic`, whose CRUD factory also owns `/sessions`.
    app.include_router(session_routes.router, prefix=API_PREFIX)
    app.include_router(imports.router, prefix=API_PREFIX)
    app.include_router(dashboards.router, prefix=API_PREFIX)
    app.include_router(notifications.router, prefix=API_PREFIX)
    app.include_router(change_requests.router, prefix=API_PREFIX)
    # Before `scopes`, so `/users` is matched by its own router rather
    # than by the scopes router's `/users/{id}/scopes` prefix.
    app.include_router(users.router, prefix=API_PREFIX)
    app.include_router(scopes.router, prefix=API_PREFIX)

    return app
