"""
SQLite engine and session management — Phase 6.

The project already standardises on SQLite for session state and evidence
storage (see AGENTS.md), so Phase 6 persists workflow runs and workflow events
in the same file.  No external database server, no Docker, no ORM service
container.

Usage::

    from app.core.db import session_scope

    with session_scope() as session:
        ...

The engine is created lazily and cached, so importing this module never
touches the filesystem.  ``configure_database()`` re-points the engine at a
different file (used by the test suite) and ``reset_database()`` drops the
cached engine entirely.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from app.core.config import settings

# Importing the schema module registers the tables on SQLModel.metadata.
from app.models import schemas  # noqa: F401

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_engine_path: str | None = None


def _normalise(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


def create_db_and_tables(path: str | Path | None = None) -> Engine:
    """Create (or return the cached) engine and ensure the tables exist.

    Args:
        path: SQLite file path.  Defaults to ``settings.database_path``.

    Returns:
        The cached ``Engine`` for that path.
    """
    global _engine, _engine_path

    if path is None and _engine is not None:
        # An engine is already open: keep using it rather than silently
        # re-pointing the database at the configured default.
        return _engine

    target = _normalise(path if path is not None else settings.database_path)

    if _engine is not None and _engine_path == target:
        return _engine

    if _engine is not None:
        _engine.dispose()
        _engine = None
        _engine_path = None

    parent = Path(target).parent
    parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{target}",
        # check_same_thread=False: the SSE endpoint and the TestClient both
        # touch the database from worker threads.  Access is serialised by
        # SQLAlchemy's connection pool and short-lived transactions.
        connect_args={"check_same_thread": False, "timeout": 10},
    )
    SQLModel.metadata.create_all(engine)

    _engine = engine
    _engine_path = target
    logger.info("Workflow database ready at %s", target)
    return engine


def get_engine() -> Engine:
    """Return the workflow database engine, creating it on first use."""
    return create_db_and_tables()


def configure_database(path: str | Path) -> Engine:
    """Point the workflow database at ``path``.

    Used by the test suite to isolate each test in its own SQLite file.  If the
    engine is already open on a different file it is disposed and rebuilt.
    """
    return create_db_and_tables(path)


def get_database_path() -> str:
    """Return the resolved path of the active workflow database file."""
    if _engine_path is not None:
        return _engine_path
    return _normalise(settings.database_path)


def reset_database() -> None:
    """Dispose of the cached engine.  The next call re-creates it."""
    global _engine, _engine_path
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_path = None


@contextmanager
def session_scope() -> Iterator[Session]:
    """Yield a committed-on-success, rolled-back-on-error session."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
