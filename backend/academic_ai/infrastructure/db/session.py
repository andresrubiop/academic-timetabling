"""Engine and session factory.

One engine per process, created lazily. The API creates it at startup; the
worker creates it when it first needs a database; a test creates its own and
never touches this module.

`pool_pre_ping` because containers restart. Postgres going away for ten
seconds during a `docker compose restart db` otherwise leaves every pooled
connection dead, and the first request after it fails with a stale-socket
error that looks nothing like the cause.
"""

from __future__ import annotations

import os
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

__all__ = ["dispose_engine", "get_engine", "session_scope", "session_factory"]

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_engine(url: str | None = None) -> Engine:
    global _engine, _factory
    if _engine is None:
        url = url or os.environ.get("DATABASE_URL", "").strip()
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set. The API and worker both refuse to "
                "run without a database rather than starting and failing on "
                "the first request."
            )
        _engine = create_engine(
            url,
            pool_pre_ping=True,
            # Modest: the worker holds a connection for the length of a solve,
            # and this host is shared. A large pool here is memory taken from
            # other tenants to sit idle.
            pool_size=5,
            max_overflow=5,
            pool_recycle=1800,
        )
        _factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _factory is not None
    return _factory


def session_scope() -> Iterator[Session]:
    """A session that commits on success and rolls back on any exception.

    The rollback is the point. A request that raises halfway through leaves a
    partial write visible to the next one otherwise, and "the run row exists
    but was never queued" is exactly the inconsistency that produces a job
    nobody ever executes.
    """
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None
