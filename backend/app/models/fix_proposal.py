"""
Fix-proposal domain types — Phase 9 (fix proposal and diff generation).

These are the *domain* half of Phase 9.  They are pure Pydantic models with no
I/O, no clock and no LLM dependency, following the convention established by
``app.models.domain``, ``app.models.workflow``, ``app.models.investigation`` and
``app.models.root_cause``.

Epistemic honesty
-----------------
A fix proposal is the most dangerous artefact in the system, because a wrong one
looks exactly like a right one.  Three rules follow from that:

* A proposal is only ever a **proposal**.  ``tests_executed`` is always ``False``
  and there is no field anywhere in this model that could carry a test exit code.
  Phase 11's sandboxed executor owns ``VERIFIED_RESULT``; nothing here may
  pre-empt it.
* ``INSUFFICIENT_EVIDENCE`` and ``INVALID`` are first-class outcomes with empty
  ``changes`` and a ``None`` diff.  A model that could express "no fix" only by
  leaving fields blank would let an empty diff be rendered as an empty fix.
* ``risk`` is a named level with the factors that produced it, never a bare
  score, so a reviewer can disagree with a specific claim instead of a number.

Determinism
-----------
Like :class:`app.models.root_cause.RootCauseAnalysis`, the proposal carries **no
timestamp**: two runs that reach the same root cause against the same source
produce a byte-identical model.  Wall-clock information is reported separately,
in the workflow event payload and in
``run.context["fix_proposal_created_at"]``.

For the same reason there is no random ``proposal_id``.  A UUID would make every
proposal unique and therefore incomparable, which defeats the replay and caching
guarantee above.  A proposal is identified by its ``run_id``, exactly as a
Phase 8 analysis is.

What Phase 9 is allowed to do
-----------------------------
Phase 9 reads the source text of the file its root cause points at, in order to
build a diff that reflects what is *actually* on disk rather than what the
evidence excerpt happened to say.  That read is deliberately the only new
capability Phase 9 has, and it is constrained: repository-relative paths only,
inside the run's own root, never a secret file, never a binary, never over the
size limit.  Writing is not available at any level — see
``app.analysis.fix_proposer``.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.models.root_cause import (
    ConfidenceLevel,
    EvidenceRef,
    RootCauseKind,
    SourceAnchor,
)

# ── Status ────────────────────────────────────────────────────────────────────


class FixProposalStatus(str, Enum):
    """What Phase 9 was able to propose.

    Mirrors the reasoning in :class:`app.models.root_cause.RootCauseStatus`: the
    interesting outcomes are the negative ones, and they are named so a client
    cannot collapse "I found nothing to change" into "I changed nothing".
    """

    #: A concrete, minimal change was drafted and is ready for a human to read.
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    #: The root cause is real but not actionable enough to change safely, or no
    #: root cause was determined at all.  No change and no diff.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    #: A root cause *was* determined, but the source it points at could not be
    #: trusted to build a diff against: the anchor is missing, stale, ambiguous,
    #: outside the repository, or in a file Phase 9 refuses to read.  No change
    #: and no diff.  Distinct from ``INSUFFICIENT_EVIDENCE`` because the remedy
    #: is different — re-run the analysis, rather than investigate further.
    INVALID = "INVALID"


# ── Risk ──────────────────────────────────────────────────────────────────────


class RiskLevel(str, Enum):
    """How much damage a wrong application of this proposal would do."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class RiskAssessment(BaseModel):
    """A named risk level plus the factors that produced it.

    ``factors`` is an ordered list of human-readable reasons, not a score.  A
    reviewer who thinks ``MEDIUM`` is wrong can point at the specific factor
    they disagree with.
    """

    level: RiskLevel = RiskLevel.LOW
    factors: list[str] = Field(default_factory=list)
    rationale: str = ""


# ── The change ────────────────────────────────────────────────────────────────


class ChangeType(str, Enum):
    """How a proposed change alters a file.

    Only replacement of existing lines is expressible.  Deleting a file, adding a
    new one or rewriting a whole module are deliberately *not* in this enum: they
    cannot be produced by the deterministic engine, and an enum that could name
    them would let a future writer imply it had.
    """

    MODIFY = "MODIFY"


class FixChange(BaseModel):
    """One concrete, minimal edit to one line (or one contiguous run of lines).

    ``before`` and ``after`` are the exact text, so a reviewer can see the change
    without applying the diff, and so the diff can be regenerated and compared.
    """

    file_path: str
    change_type: ChangeType = ChangeType.MODIFY
    line: int
    line_end: int | None = None
    function: str | None = None
    before: str
    after: str
    description: str = ""


# ── Where the root cause points ───────────────────────────────────────────────


class AnchorLocation(BaseModel):
    """Where the root cause points — the location, never the content.

    Deliberately *not* :class:`app.models.root_cause.SourceAnchor`.  That model
    carries a verbatim ``excerpt`` of the offending source, which is what Phase 8
    needs.  A proposal does not: the proposed ``before``/``after`` text and the
    unified diff already show the change, and any excerpt Phase 9 copied would
    be duplicated source text that has to be re-validated for secrecy every time
    a proposal is rendered, logged or returned over HTTP.  Keeping the excerpt
    out of the model removes that whole class of leak instead of auditing it.

    The file path and line are still enough for a reviewer to jump to the code.
    """

    file_path: str
    line: int | None = None
    function: str | None = None

    @classmethod
    def from_anchor(cls, anchor: SourceAnchor | None) -> "AnchorLocation | None":
        """Reduce a Phase 8 anchor to a location, dropping the excerpt."""
        if anchor is None:
            return None
        return cls(
            file_path=anchor.file_path,
            line=anchor.line,
            function=anchor.function,
        )


# ── Validation plan ───────────────────────────────────────────────────────────


class ValidationPlan(BaseModel):
    """What a developer should check.  Never what was checked.

    ``tests_executed`` is fixed at ``False`` by the engine.  The field exists so
    the honesty is *visible* in the API rather than merely true in the code: a
    client cannot read a plan and assume the checks were run.
    """

    steps: list[str] = Field(default_factory=list)
    tests_executed: bool = False
    notes: str = ""


# ── The proposal ──────────────────────────────────────────────────────────────


class FixProposal(BaseModel):
    """Phase 9's proposal for one run.

    Invariants enforced by construction in ``app.analysis.fix_proposer`` and
    re-checked by the API layer:

    * ``status is READY_FOR_REVIEW`` if and only if ``unified_diff`` is a
      non-empty string.  A ready proposal with no diff, or a diff with no ready
      status, is always a bug.
    * A non-ready proposal has no ``changes`` and no diff.
    * ``tests_executed`` is always ``False``.
    """

    run_id: str
    issue: str
    status: FixProposalStatus

    #: One-paragraph explanation of the proposed change, or of why none was made.
    rationale: str

    # ── Traceability back to Phase 8 ───────────────────────────────────
    #: ``None`` unless Phase 8 determined a root cause.  Copied rather than
    #: re-derived so a client can read the proposal without a second lookup, and
    #: so the proposal cannot silently disagree with the analysis it came from.
    root_cause: str | None = None
    root_cause_kind: RootCauseKind | None = None
    root_cause_confidence: float = 0.0
    root_cause_confidence_level: ConfidenceLevel = ConfidenceLevel.LOW
    root_cause_status: str = ""
    #: Where the root cause points, reduced to a location.  See
    #: :class:`AnchorLocation` for why the source excerpt is not carried over.
    anchor: AnchorLocation | None = None
    #: Citations backing the root cause.  ``EvidenceRef`` copies only identifying
    #: fields, so the run's evidence store stays the single source of truth.
    supporting_evidence: list[EvidenceRef] = Field(default_factory=list)

    # ── The change ─────────────────────────────────────────────────────
    affected_files: list[str] = Field(default_factory=list)
    changes: list[FixChange] = Field(default_factory=list)
    #: A standard unified diff, generated in memory.  ``None`` — never an empty
    #: string — when no change is proposed.
    unified_diff: str | None = None

    # ── Review metadata ────────────────────────────────────────────────
    risk: RiskAssessment = Field(default_factory=RiskAssessment)
    validation_plan: ValidationPlan = Field(default_factory=ValidationPlan)
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)

    #: Always False in Phase 9.  Exposed so a client cannot mistake a static
    #: reading plus a hand-built diff for a fix that was tried and worked.
    tests_executed: bool = False

    # ── Persistence helpers ─────────────────────────────────────────────
    #
    # Stored in ``MentorRun.context`` alongside the Phase 8 analysis.  The model
    # is timestamp-free by design, so this is a total, lossless representation
    # and re-reading it yields an identical object.

    def to_summary(self) -> dict[str, Any]:
        """Serialise the proposal for ``run.context``."""
        return self.model_dump(mode="json")

    @classmethod
    def from_summary(cls, run_id: str, summary: dict[str, Any]) -> "FixProposal":
        """Rebuild a proposal previously written by :meth:`to_summary`."""
        data = dict(summary)
        data["run_id"] = run_id
        return cls.model_validate(data)

    def evidence_ids(self) -> list[str]:
        """Stable evidence IDs backing this proposal, in citation order."""
        return [ref.evidence_id for ref in self.supporting_evidence]

    def is_ready_for_review(self) -> bool:
        """True when a concrete diff exists and is waiting for a human."""
        return (
            self.status is FixProposalStatus.READY_FOR_REVIEW
            and bool(self.unified_diff)
        )

    def added_line_count(self) -> int:
        """Lines added by the diff.  Purely a reporting helper for the UI."""
        return _count_diff_lines(self.unified_diff, "+")

    def removed_line_count(self) -> int:
        """Lines removed by the diff.  Purely a reporting helper for the UI."""
        return _count_diff_lines(self.unified_diff, "-")


def _count_diff_lines(diff: str | None, marker: str) -> int:
    """Count diff body lines starting with ``marker``, ignoring file headers."""
    if not diff:
        return 0
    count = 0
    for line in diff.splitlines():
        # ``---``/``+++`` are the file headers, not content.
        if line.startswith(marker * 3):
            continue
        if line.startswith(marker) and not line.startswith(marker * 2):
            count += 1
    return count


__all__ = [
    "AnchorLocation",
    "ChangeType",
    "FixChange",
    "FixProposal",
    "FixProposalStatus",
    "RiskAssessment",
    "RiskLevel",
    "ValidationPlan",
]
