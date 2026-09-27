"""
Workflow event store — Phase 6.

An append-only, SQLite-backed log of workflow events.

Guarantees
----------
* APPEND-ONLY — this module exposes no update and no delete operation.  Once an
  event is written it is never modified.
* ORDERED — ``sequence`` is a gap-free per-run counter starting at 1.  Reads
  always sort by it, and it doubles as the SSE resume cursor.
* RUN-SCOPED — every read filters on ``run_id``, so one run can never observe
  another run's events.

No external message broker (Redis, Kafka, RabbitMQ, …) is used or required.
The SSE endpoint tails this store directly, which keeps the stream correct
across reconnects and server restarts.
"""

from __future__ import annotations

import json
import logging
from contextlib import AbstractContextManager
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlmodel import func, select

from app.core.db import session_scope
from app.models.schemas import WorkflowEventRecord
from app.models.workflow import (
    RunStatus,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowState,
)

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractContextManager]

#: How many events a single read may return.
DEFAULT_PAGE_SIZE = 500

#: Upper bound on a single sequence allocation retry loop.
_MAX_SEQUENCE_RETRIES = 5


class EventStore:
    """Append-only event log backed by SQLite."""

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._session_factory: SessionFactory = session_factory or session_scope

    # ── Writes (append only) ──────────────────────────────────────────────

    def append(
        self,
        run_id: str,
        event_type: WorkflowEventType,
        state: WorkflowState,
        status: RunStatus,
        message: str,
        payload: dict | None = None,
        event_id: str | None = None,
        sequence: int | None = None,
    ) -> WorkflowEvent:
        """Append an event to ``run_id``'s log and return the stored event.

        The ``sequence`` and ``event_id`` are assigned here so that ordering is
        owned by the store rather than by the caller.  ``event_id`` and
        ``sequence`` may be supplied explicitly, but only by code that needs
        reproducible identifiers in tests.
        """
        from app.models.workflow import new_event_id

        for attempt in range(1, _MAX_SEQUENCE_RETRIES + 1):
            with self._session_factory() as session:
                next_sequence = (
                    sequence
                    if sequence is not None
                    else self._next_sequence(session, run_id)
                )
                record = WorkflowEventRecord(
                    event_id=event_id or new_event_id(),
                    run_id=run_id,
                    sequence=next_sequence,
                    event_type=event_type.value,
                    state=state.value,
                    status=status.value,
                    message=message,
                    payload=json.dumps(payload or {}),
                    timestamp=_now_iso(),
                )
                session.add(record)
                try:
                    session.commit()
                except IntegrityError:
                    # Another writer claimed the same sequence — retry.
                    session.rollback()
                    logger.debug(
                        "Sequence collision for run %s (attempt %s/%s)",
                        run_id,
                        attempt,
                        _MAX_SEQUENCE_RETRIES,
                    )
                    continue
                return _to_domain(record)

        raise RuntimeError(
            f"Could not allocate an event sequence for run {run_id} after "
            f"{_MAX_SEQUENCE_RETRIES} attempts"
        )

    # ── Reads ─────────────────────────────────────────────────────────────

    def list(
        self,
        run_id: str,
        after_sequence: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
    ) -> list[WorkflowEvent]:
        """Return events for ``run_id`` with ``sequence > after_sequence``.

        Results are always ordered by ascending sequence.  Events belonging to
        other runs are never returned.
        """
        with self._session_factory() as session:
            statement = (
                select(WorkflowEventRecord)
                .where(WorkflowEventRecord.run_id == run_id)
                .where(WorkflowEventRecord.sequence > after_sequence)
                .order_by(WorkflowEventRecord.sequence.asc())
                .limit(limit)
            )
            return [_to_domain(r) for r in session.exec(statement).all()]

    def count(self, run_id: str) -> int:
        """Return the number of events recorded for ``run_id``."""
        with self._session_factory() as session:
            statement = select(func.count()).where(
                WorkflowEventRecord.run_id == run_id
            )
            return int(session.exec(statement).one())

    def latest_sequence(self, run_id: str) -> int:
        """Return the highest sequence for ``run_id``, or 0 when empty."""
        with self._session_factory() as session:
            statement = select(func.max(WorkflowEventRecord.sequence)).where(
                WorkflowEventRecord.run_id == run_id
            )
            value = session.exec(statement).one()
            return int(value or 0)

    def get_by_event_id(self, event_id: str) -> WorkflowEvent | None:
        """Look up a single event by its ID (used for SSE resume)."""
        with self._session_factory() as session:
            record = session.get(WorkflowEventRecord, event_id)
            return _to_domain(record) if record else None

    def sequence_of(self, event_id: str) -> int | None:
        """Return the sequence of ``event_id``, or None when unknown."""
        with self._session_factory() as session:
            statement = select(WorkflowEventRecord.sequence).where(
                WorkflowEventRecord.event_id == event_id
            )
            value = session.exec(statement).first()
            return int(value) if value is not None else None

    # ── private ───────────────────────────────────────────────────────────

    @staticmethod
    def _next_sequence(session, run_id: str) -> int:
        statement = select(func.max(WorkflowEventRecord.sequence)).where(
            WorkflowEventRecord.run_id == run_id
        )
        current = session.exec(statement).one()
        return int(current or 0) + 1


# ── Mapping helpers ───────────────────────────────────────────────────────────


def _to_domain(record: WorkflowEventRecord) -> WorkflowEvent:
    return WorkflowEvent(
        event_id=record.event_id,
        run_id=record.run_id,
        sequence=record.sequence,
        event_type=WorkflowEventType(record.event_type),
        state=WorkflowState(record.state),
        status=RunStatus(record.status),
        message=record.message,
        timestamp=_from_iso(record.timestamp),
        payload=_parse_payload(record.payload),
    )


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _from_iso(value: str):  # -> datetime
    from datetime import datetime

    return datetime.fromisoformat(value)


def _parse_payload(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.error("Discarding unparseable event payload: %s", exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}
