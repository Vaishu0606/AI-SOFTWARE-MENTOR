"""
Core domain types for the AI Software Mentor.

These types represent the fundamental concepts that flow through the
investigation workflow. They are intentionally decoupled from persistence
(SQLModel) and transport (Pydantic HTTP schemas) concerns.

Evidence type hierarchy:
    OBSERVATION      → fact directly read from files or tool output
    HYPOTHESIS       → reasoned inference; may be wrong; labeled clearly
    PROPOSED_ACTION  → fix or test awaiting human review and approval
    VERIFIED_RESULT  → confirmed by actual test execution with exit code
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


# ── Evidence ──────────────────────────────────────────────────────────────────


class EvidenceType(str, Enum):
    """Classifies the epistemic status of an evidence item.

    A HYPOTHESIS must never be promoted to VERIFIED_RESULT without actual
    test execution. The Orchestrator enforces this in the evidence store.
    """

    OBSERVATION = "OBSERVATION"
    HYPOTHESIS = "HYPOTHESIS"
    PROPOSED_ACTION = "PROPOSED_ACTION"
    VERIFIED_RESULT = "VERIFIED_RESULT"


class Evidence(BaseModel):
    """A single piece of evidence collected during an investigation."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    evidence_type: EvidenceType
    agent: str  # name of the agent that produced this evidence
    title: str
    description: str
    source_file: str | None = None  # repository-relative path, if applicable
    source_line: int | None = None
    raw_content: str | None = None  # the actual code snippet or output
    confidence: float | None = None  # 0.0 – 1.0, for HYPOTHESIS items
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] = Field(default_factory=dict)


# ── Repository ────────────────────────────────────────────────────────────────


class FileInfo(BaseModel):
    """Lightweight metadata for a single file discovered during exploration."""

    relative_path: str
    extension: str
    language: str
    size_bytes: int
    is_test: bool
    is_config: bool
    is_secret: bool


class RepositoryInfo(BaseModel):
    """Describes the repository under investigation.

    Phase 1 fields are unchanged; Phase 3 adds optional exploration fields.
    All new fields are Optional so existing code constructing RepositoryInfo
    without them continues to work without modification.
    """

    # Phase 1 fields (unchanged)
    url: str | None = None  # GitHub URL (None for local path)
    local_path: str | None = None  # absolute path to local clone
    name: str | None = None
    primary_language: str | None = None
    frameworks: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    relevant_files: list[str] = Field(default_factory=list)
    test_files: list[str] = Field(default_factory=list)

    # Phase 3 fields (all optional — not breaking)
    total_files: int | None = None
    directories: list[str] = Field(default_factory=list)
    all_files: list[FileInfo] = Field(default_factory=list)
    config_files: list[str] = Field(default_factory=list)
    languages: dict[str, int] = Field(default_factory=dict)
    skipped_files: list[dict[str, str]] = Field(default_factory=list)


# ── Investigation ─────────────────────────────────────────────────────────────


class Hypothesis(BaseModel):
    """A candidate explanation for the reported bug."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    title: str
    description: str
    supporting_evidence: list[str] = Field(default_factory=list)  # Evidence IDs
    confidence: float  # 0.0 – 1.0
    rank: int | None = None  # 1 = most likely


class Incident(BaseModel):
    """The developer-reported bug or issue being investigated."""

    title: str
    description: str  # natural-language bug report
    reporter: str | None = None
    repository_url: str | None = None


class Investigation(BaseModel):
    """The current state of an active investigation session."""

    session_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    incident: Incident
    repository: RepositoryInfo = Field(default_factory=RepositoryInfo)
    evidence: list[Evidence] = Field(default_factory=list)
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    status: str = "IDLE"  # matches Orchestrator state machine states
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ── Fix Proposal ──────────────────────────────────────────────────────────────


class FixProposal(BaseModel):
    """A proposed minimal fix produced by the Fix Agent."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    hypothesis_id: str  # the hypothesis this fix addresses
    unified_diff: str  # standard unified diff format
    files_affected: list[str] = Field(default_factory=list)
    explanation: str  # why this addresses the root cause
    approved: bool = False
    approved_at: datetime | None = None


# ── Test Result ───────────────────────────────────────────────────────────────


class InvestigationTestResult(BaseModel):
    """The result of executing tests via the Verification Agent."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    session_id: str
    test_files: list[str] = Field(default_factory=list)
    exit_code: int
    stdout: str
    stderr: str
    passed: bool  # True only when exit_code == 0
    executed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @classmethod
    def from_execution(
        cls,
        session_id: str,
        test_files: list[str],
        exit_code: int,
        stdout: str,
        stderr: str,
    ) -> "TestResult":
        """Construct a TestResult from actual subprocess output.

        passed is derived from exit_code — never set manually.
        """
        return cls(
            session_id=session_id,
            test_files=test_files,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            passed=(exit_code == 0),
        )


# ── Agent Result ──────────────────────────────────────────────────────────────


class AgentResult(BaseModel):
    """The typed output produced by any agent after completing its task."""

    agent: str
    session_id: str
    success: bool
    evidence_items: list[Evidence] = Field(default_factory=list)
    error: str | None = None  # set only when success=False
    metadata: dict[str, Any] = Field(default_factory=dict)
