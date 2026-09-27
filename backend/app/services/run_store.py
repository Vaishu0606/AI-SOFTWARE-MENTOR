"""
Run store — Phase 6.

Persistence for ``MentorRun`` on top of SQLite (``app.core.db``).

The store is deliberately thin: it maps rows to domain models and back, and
nothing else.  All workflow rules live in ``app.services.orchestrator`` and
``app.services.state_machine``.

Security: ``repository_path`` is stored exactly as supplied by the
orchestrator, which validates it through the Phase 3 ``RepositoryCloner``
before the run is ever created.
"""

from __future__ import annotations

import json
import logging
from contextlib import AbstractContextManager
from typing import Callable

from pydantic import ValidationError
from sqlmodel import select

from app.core.db import session_scope
from app.models.domain import Evidence
from app.models.schemas import MentorRunRecord
from app.models.workflow import MentorRun, RunStatus, WorkflowState

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractContextManager]


class RunNotFoundError(Exception):
    """Raised when a run ID does not resolve to a stored run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        super().__init__(f"Run not found: {run_id}")


class RunStore:
    """Read/write access to persisted workflow runs."""

    def __init__(
        self, session_factory: SessionFactory | None = None
    ) -> None:
        """
        Args:
            session_factory: Callable returning a session context manager.
                Defaults to :func:`app.core.db.session_scope`.  Injected in
                tests to point at an isolated database.
        """
        self._session_factory: SessionFactory = session_factory or session_scope

    # ── Writes ────────────────────────────────────────────────────────────

    def create(self, run: MentorRun) -> MentorRun:
        """Insert a new run.  The caller supplies the run ID and timestamps."""
        with self._session_factory() as session:
            session.add(_to_record(run))
        return run

    def save(self, run: MentorRun) -> MentorRun:
        """Overwrite the mutable fields of an existing run."""
        with self._session_factory() as session:
            record = session.get(MentorRunRecord, run.run_id)
            if record is None:
                raise RunNotFoundError(run.run_id)
            record.repository_path = run.repository_path
            record.issue = run.issue
            record.current_state = run.current_state.value
            record.status = run.status.value
            record.error = run.error
            record.evidence = json.dumps(
                [e.model_dump(mode="json") for e in run.evidence]
            )
            record.context = json.dumps(run.context)
            record.updated_at = run.updated_at.isoformat()
            session.add(record)
        return run

    # ── Reads ─────────────────────────────────────────────────────────────

    def get(self, run_id: str) -> MentorRun:
        """Return the run for ``run_id`` or raise ``RunNotFoundError``."""
        with self._session_factory() as session:
            record = session.get(MentorRunRecord, run_id)
            if record is None:
                raise RunNotFoundError(run_id)
            return _to_domain(record)

    def exists(self, run_id: str) -> bool:
        """Return True when ``run_id`` is stored."""
        with self._session_factory() as session:
            return session.get(MentorRunRecord, run_id) is not None

    def list_runs(self, limit: int = 100) -> list[MentorRun]:
        """Return runs, newest first.  Convenience for local inspection."""
        with self._session_factory() as session:
            statement = (
                select(MentorRunRecord)
                .order_by(MentorRunRecord.created_at.desc())
                .limit(limit)
            )
            return [_to_domain(r) for r in session.exec(statement).all()]


# ── Mapping helpers ───────────────────────────────────────────────────────────


def _to_record(run: MentorRun) -> MentorRunRecord:
    return MentorRunRecord(
        run_id=run.run_id,
        repository_path=run.repository_path,
        issue=run.issue,
        current_state=run.current_state.value,
        status=run.status.value,
        error=run.error,
        evidence=json.dumps([e.model_dump(mode="json") for e in run.evidence]),
        context=json.dumps(run.context),
        created_at=run.created_at.isoformat(),
        updated_at=run.updated_at.isoformat(),
    )


def _to_domain(record: MentorRunRecord) -> MentorRun:
    return MentorRun(
        run_id=record.run_id,
        repository_path=record.repository_path,
        issue=record.issue,
        current_state=WorkflowState(record.current_state),
        status=RunStatus(record.status),
        error=record.error,
        created_at=_parse_timestamp(record.created_at),
        updated_at=_parse_timestamp(record.updated_at),
        evidence=_parse_evidence(record.evidence),
        context=_parse_context(record.context),
    )


def _parse_timestamp(value: str):  # -> datetime
    from datetime import datetime

    return datetime.fromisoformat(value)


def _parse_evidence(raw: str) -> list[Evidence]:
    if not raw:
        return []
    try:
        return [Evidence.model_validate(item) for item in json.loads(raw)]
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        # Never let a corrupt row break run inspection.
        logger.error("Discarding unparseable evidence for run: %s", exc)
        return []


def _parse_context(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Discarding unparseable run context: %s", exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}
