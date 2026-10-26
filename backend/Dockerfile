# Backend + optimizer worker. One image, two commands: the API and the worker
# run identical code and must not drift, and a second image would double the
# build cache on a filesystem with 9 GiB free.

# ---------------------------------------------------------------------------
# Builder — wheels here, so the runtime image carries no compiler.
# ---------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# gcc is needed to build argon2-cffi's bindings where no wheel matches. It
# stays in this stage: a compiler in the runtime image turns a code-execution
# bug into a toolchain for the attacker.
RUN apt-get update \
 && apt-get install --no-install-recommends -y build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
# Copied before the source so the dependency layer is cached across code
# changes. Without this every edit re-downloads OR-Tools, which is the
# largest single thing in this image.
COPY academic_ai/__init__.py academic_ai/__init__.py

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PATH="/opt/venv/bin:$PATH"

# A fixed uid/gid rather than whatever the base image picks: the container
# runs read-only with no-new-privileges, and a known non-root uid is what
# makes a bind-mounted volume's ownership predictable later.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app

# libgomp is OR-Tools' OpenMP runtime. Without it CP-SAT imports and then
# fails at first solve -- the worker healthcheck builds a real model
# specifically to catch that at deploy time rather than at 02:00.
RUN apt-get update \
 && apt-get install --no-install-recommends -y libgomp1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=10001:10001 academic_ai ./academic_ai
COPY --chown=10001:10001 alembic ./alembic
COPY --chown=10001:10001 alembic.ini ./alembic.ini

USER 10001:10001

EXPOSE 8000

# The API answers /healthz itself; the generic check covers the worker too,
# which has no HTTP surface.
HEALTHCHECK --interval=15s --timeout=10s --start-period=20s --retries=3 \
    CMD ["python", "-m", "academic_ai.health"]

# The default is the API; compose overrides it for the worker. One image, two
# commands, so the two can never drift in what they are able to import.
#
# --proxy-headers because nginx terminates every connection and the client IP
# recorded on a session would otherwise always be the proxy's.
CMD ["uvicorn", "academic_ai.api.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", \
     "--no-access-log"]
