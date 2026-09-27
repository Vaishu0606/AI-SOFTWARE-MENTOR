"""
SQLModel persistence schemas — Phase 6.

Two append-mostly tables back the Phase 6 orchestrator:

``mentor_runs``
    One row per workflow run.  Holds the repository reference, the developer
    issue, the current state/status, error information and a JSON snapshot of
    the run context and evidence.

``workflow_events``
    Append-only event log.  Rows are never updated or deleted.  ``sequence`` is
    a gap-free per-run counter that defines event order and doubles as the SSE
    resume cursor.

The project targets SQLite.  No external database server is required.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class MentorRunRecord(SQLModel, table=True):
    """A persisted workflow run."""

    __tablename__ = "mentor_runs"

    run_id: str = Field(primary_key=True, index=True)
    repository_path: str
    issue: str
    current_state: str = Field(index=True)
    status: str = Field(index=True)
    error: str | None = None
    #: JSON-encoded list[dict] — typed as Evidence on the way in and out.
    evidence: str = Field(default="[]")
    #: JSON-encoded dict of stage-produced summary data.
    context: str = Field(default="{}")
    #: ISO-8601 UTC timestamps (stored as text for stable ordering).
    created_at: str
    updated_at: str


class WorkflowEventRecord(SQLModel, table=True):
    """An append-only workflow event."""

    __tablename__ = "workflow_events"
    __table_args__ = (
        UniqueConstraint(
            "run_id", "sequence", name="uq_workflow_events_run_sequence"
        ),
    )

    event_id: str = Field(primary_key=True, index=True)
    run_id: str = Field(index=True)
    sequence: int
    event_type: str
    state: str
    status: str
    message: str
    #: JSON-encoded dict payload.
    payload: str = Field(default="{}")
    timestamp: str
