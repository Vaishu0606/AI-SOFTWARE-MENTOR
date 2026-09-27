"""
Investigation domain types — Phase 7 (parallel bug investigation).

These are the *domain* half of Phase 7, following the convention established by
``app.models.domain`` and ``app.models.workflow``: pure Pydantic models,
decoupled from persistence and transport.

Scope and honesty rules
-----------------------
Phase 7 investigates a reported bug and produces **evidence**.  It deliberately
does *not* decide what went wrong:

* every investigator emits ``OBSERVATION`` for facts read from the repository
  and ``HYPOTHESIS`` for reasoning about those facts,
* ``PROPOSED_ACTION`` is allowed for investigative next steps ("reproduce this
  combination in a test") but never for a fix,
* ``VERIFIED_RESULT`` is **forbidden** — nothing is executed in Phase 7, so no
  result can be verified.  :func:`app.investigation.evidence.build_evidence`
  raises if an investigator tries,
* the root cause is declared by Phase 8 (evidence synthesis), not here.

Determinism
-----------
Nothing in this module, and nothing in ``app.investigation`` as a whole, calls an
LLM, a clock-dependent branch, or a random source.  The same repository plus the
same issue always produce the same investigation.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.models.domain import Evidence, EvidenceType
from app.models.workflow import utc_now


# ── Investigators ──────────────────────────────────────────────────────────────


class InvestigatorName(str, Enum):
    """The three independent Phase 7 investigators.

    The declaration order below is the *canonical order* used everywhere results
    are presented, so output is deterministic regardless of the order in which
    the investigators actually finish.
    """

    CODE = "code_investigator"
    TEST = "test_investigator"
    DEPENDENCY = "dependency_investigator"

    @property
    def label(self) -> str:
        """Human-readable name for messages and reports."""
        return {
            InvestigatorName.CODE: "Code Investigator",
            InvestigatorName.TEST: "Test Investigator",
            InvestigatorName.DEPENDENCY: "Dependency Investigator",
        }[self]


class InvestigatorStatus(str, Enum):
    """Lifecycle status of a single investigator."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class InvestigationStatus(str, Enum):
    """Overall status of one investigation.

    COMPLETED
        Every investigator finished successfully.
    PARTIAL
        At least one investigator succeeded and at least one failed.  The
        successful evidence is kept and the failure is reported.
    FAILED
        No investigator produced evidence.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


# ── Source traceability ────────────────────────────────────────────────────────


class SourceLocation(BaseModel):
    """A precise, repository-relative pointer to a piece of code.

    ``file_path`` is always repository-relative and POSIX-style so evidence is
    portable between machines and never leaks an absolute host path.
    """

    file_path: str
    line_start: int | None = None
    line_end: int | None = None

    def __str__(self) -> str:
        if self.line_start is None:
            return self.file_path
        if self.line_end is not None and self.line_end != self.line_start:
            return f"{self.file_path}:{self.line_start}-{self.line_end}"
        return f"{self.file_path}:{self.line_start}"


class SourceReference(BaseModel):
    """A secondary location that supports an evidence item.

    Used when a single statement is only fully supported by looking at more than
    one place — for example "route A calls service B" needs the route line *and*
    the service definition line.
    """

    file_path: str
    line: int | None = None
    detail: str = ""


# ── Configuration ──────────────────────────────────────────────────────────────


class InvestigationConfig(BaseModel):
    """Deterministic limits for one investigation.

    Every value exists to bound work and output size on large repositories.  The
    defaults are sized for a small application (tens of files); a repository
    with thousands of files will be capped rather than exhaustively scanned.
    """

    #: How many repository files an investigator may treat as "issue relevant".
    max_relevant_files: int = 12

    #: Hard cap on evidence items produced by a single investigator.
    max_evidence_per_investigator: int = 40

    #: How many test files the Test Investigator inspects in detail.
    max_test_files: int = 20

    #: How many source lines of context may be quoted as supporting content.
    max_snippet_lines: int = 12

    #: How many dependency edges / relationships are reported per investigator.
    max_relationships: int = 30

    def model_post_init(self, _context: Any) -> None:  # noqa: D401 - pydantic hook
        """Reject nonsensical limits early rather than mid-investigation."""
        for name in (
            "max_relevant_files",
            "max_evidence_per_investigator",
            "max_test_files",
            "max_snippet_lines",
            "max_relationships",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")


# ── Per-investigator report ────────────────────────────────────────────────────


class InvestigatorReport(BaseModel):
    """The outcome of one investigator.

    ``evidence_count`` is denormalised so a caller can render a summary without
    loading every evidence item.
    """

    investigator: InvestigatorName
    status: InvestigatorStatus
    #: Monotonic seconds spent in ``investigate()``.  Present for reporting only
    #: — it never influences investigation content or ordering.
    duration_seconds: float = 0.0
    evidence_count: int = 0
    #: Set when ``status`` is ``FAILED``; always a human-readable reason.
    error: str | None = None
    #: Counts and names the investigator discovered, for the UI and the report.
    summary: dict[str, Any] = Field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status is InvestigatorStatus.COMPLETED


# ── Investigation result ───────────────────────────────────────────────────────


class InvestigationResult(BaseModel):
    """Everything one investigation produced.

    ``evidence`` holds the full typed evidence list.  Phase 8 will consume this
    model to perform synthesis and root-cause analysis; Phase 7 only fills it.
    """

    run_id: str
    status: InvestigationStatus = InvestigationStatus.PENDING
    investigators: list[InvestigatorReport] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    #: One entry per failed investigator: ``"<name>: <reason>"``.
    errors: list[str] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None

    def report_for(self, name: InvestigatorName) -> InvestigatorReport | None:
        """Return the report for ``name``, or None when it is absent."""
        for report in self.investigators:
            if report.investigator is name:
                return report
        return None

    def evidence_for(self, name: InvestigatorName) -> list[Evidence]:
        """Return only the evidence produced by ``name``, in production order."""
        return [e for e in self.evidence if e.agent == name.value]

    @property
    def completed_investigators(self) -> list[InvestigatorReport]:
        return [r for r in self.investigators if r.status is InvestigatorStatus.COMPLETED]

    @property
    def failed_investigators(self) -> list[InvestigatorReport]:
        return [r for r in self.investigators if r.status is InvestigatorStatus.FAILED]

    # ── Persistence helpers ───────────────────────────────────────────────
    #
    # The evidence list itself is persisted by the Phase 6 run store as part of
    # ``MentorRun.evidence``; duplicating it here would create two sources of
    # truth.  These two helpers therefore serialise everything *except* the
    # evidence, and rebuild the result from the run store on read.

    def to_summary(self) -> dict[str, Any]:
        """Serialise the investigation without its evidence list."""
        return {
            "status": self.status.value,
            "investigators": [r.model_dump(mode="json") for r in self.investigators],
            "errors": list(self.errors),
            "evidence_count": len(self.evidence),
            "started_at": self.started_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }

    @classmethod
    def from_summary(
        cls,
        run_id: str,
        summary: dict[str, Any],
        evidence: list[Evidence] | None = None,
    ) -> "InvestigationResult":
        """Rebuild a result from a stored summary plus (optional) evidence."""
        if not summary:
            return cls(run_id=run_id, status=InvestigationStatus.PENDING)
        raw_reports = summary.get("investigators") or []
        return cls(
            run_id=run_id,
            status=InvestigationStatus(summary.get("status", "PENDING")),
            investigators=[
                InvestigatorReport.model_validate(r) for r in raw_reports
            ],
            evidence=list(evidence or []),
            errors=list(summary.get("errors") or []),
            started_at=datetime.fromisoformat(summary["started_at"]),
            completed_at=(
                datetime.fromisoformat(summary["completed_at"])
                if summary.get("completed_at")
                else None
            ),
        )


#: Evidence types Phase 7 is allowed to emit.  ``VERIFIED_RESULT`` is absent by
#: design: Phase 7 executes nothing, so it verifies nothing.
PHASE7_ALLOWED_EVIDENCE_TYPES: frozenset[EvidenceType] = frozenset(
    {
        EvidenceType.OBSERVATION,
        EvidenceType.HYPOTHESIS,
        EvidenceType.PROPOSED_ACTION,
    }
)

__all__ = [
    "PHASE7_ALLOWED_EVIDENCE_TYPES",
    "InvestigationConfig",
    "InvestigationResult",
    "InvestigationStatus",
    "InvestigatorName",
    "InvestigatorReport",
    "InvestigatorStatus",
    "SourceLocation",
    "SourceReference",
]
