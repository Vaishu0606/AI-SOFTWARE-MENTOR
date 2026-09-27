"""
Root-cause domain types — Phase 8 (evidence synthesis and root-cause analysis).

These are the *domain* half of Phase 8.  They are pure Pydantic models with no
I/O, no clock and no LLM dependency, following the convention established by
``app.models.domain``, ``app.models.workflow`` and ``app.models.investigation``.

Epistemic honesty
-----------------
``RootCauseAnalysis`` is the first artefact in the system that is allowed to
name a root cause, and it is deliberately constrained:

* Every claim carries ``EvidenceRef`` values pointing at stable ``Evidence.id``
  values, so a reviewer can walk from the conclusion back to the exact source
  location that supports it.
* ``RootCauseStatus.INSUFFICIENT_EVIDENCE`` and ``NO_EVIDENCE`` are first-class,
  expected outcomes.  "I cannot determine this from the evidence" is a valid and
  important answer; a wrong root cause is worse than no root cause.
* The model **cannot express a verified result**.  There is no field that would
  let a caller attach a test exit code, because Phase 8 executes nothing.  Only
  Phase 11 (Verification) may produce ``EvidenceType.VERIFIED_RESULT``, and the
  synthesizer rejects that type on input rather than passing it through.

Determinism
-----------
The analysis is a pure function of the Phase 7 ``InvestigationResult``.  It
contains **no timestamp**: two runs with identical evidence produce a
byte-identical model.  Wall-clock information is reported separately, in the
workflow event payload and in ``run.context["root_cause_synthesized_at"]``, so
that replaying a synthesis is exactly reproducible.  This is why
``duration``/``started_at`` values from Phase 7 reports are read for reporting
only and are never allowed to influence reasoning or ordering.

Confidence mapping
------------------
``confidence`` is a score in ``[0.0, 1.0]``; ``confidence_level`` is derived from
it by :meth:`ConfidenceLevel.from_score` using these documented thresholds:

===================  ==========  ==========================================
``confidence_level``  score range  meaning
===================  ==========  ==========================================
``HIGH``              ``>= 0.75``  multiple independent investigators agree and
                                   a direct source observation backs the site
``MEDIUM``            ``>= 0.45``  a defensible reading of the evidence, with
                                   named gaps or conflicts
``LOW``               ``< 0.45``   a lead worth pursuing, not a conclusion
===================  ==========  ==========================================

A ``HIGH`` verdict additionally requires a complete investigation: the scoring
function in ``app.analysis.evidence_synthesizer`` refuses to reach ``HIGH`` when
any investigator failed, so partial evidence can never be presented as settled.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.models.domain import EvidenceType
from app.models.investigation import InvestigationStatus

# ── Status and confidence ─────────────────────────────────────────────────────


class RootCauseStatus(str, Enum):
    """What Phase 8 was able to conclude.

    These are deliberately not booleans.  "Investigated and found a defect",
    "investigated and found nothing conclusive" and "never investigated" must be
    distinguishable by a client, because collapsing them is how a debugging tool
    ends up confidently wrong.
    """

    #: A root cause was identified and is backed by cited evidence.
    DETERMINED = "DETERMINED"
    #: Evidence exists but does not identify a single defect site.
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    #: The investigation completed and produced no usable evidence.
    NO_EVIDENCE = "NO_EVIDENCE"
    #: Synthesis succeeded but one or more investigators failed, so the result
    #: covers only part of the system and confidence is reduced accordingly.
    PARTIAL = "PARTIAL"
    #: Synthesis could not be performed at all (bad or absent input).
    FAILED = "FAILED"


#: Lower bound of the ``HIGH`` confidence band.  These live at module scope
#: because a plain float assigned inside an ``Enum`` body would become an enum
#: member rather than a constant.
HIGH_CONFIDENCE_THRESHOLD = 0.75

#: Lower bound of the ``MEDIUM`` confidence band.
MEDIUM_CONFIDENCE_THRESHOLD = 0.45


class ConfidenceLevel(str, Enum):
    """Coarse confidence band derived from a numeric score."""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"

    @classmethod
    def from_score(cls, score: float) -> "ConfidenceLevel":
        """Map a ``[0, 1]`` score onto its band using the documented thresholds."""
        if score >= HIGH_CONFIDENCE_THRESHOLD:
            return cls.HIGH
        if score >= MEDIUM_CONFIDENCE_THRESHOLD:
            return cls.MEDIUM
        return cls.LOW


class RootCauseKind(str, Enum):
    """The category of defect the synthesizer is able to assert.

    The MVP recognises a small, precisely-defined set of static patterns rather
    than attempting open-ended bug classification.  ``UNRESOLVED`` is the honest
    result whenever no recognised pattern is present.
    """

    #: A numeric value is truncated/rounded *before* it is scaled, so the
    #: result is systematically wrong for values that are not exact multiples.
    TRUNCATION_BEFORE_SCALING = "TRUNCATION_BEFORE_SCALING"
    #: The reported path is never exercised by the test suite.
    UNEXERCISED_PATH = "UNEXERCISED_PATH"
    #: Two or more components of the reported path are never tested together.
    UNCOVERED_INTERACTION = "UNCOVERED_INTERACTION"
    #: No recognised pattern matched.
    UNRESOLVED = "UNRESOLVED"


class ChainNodeKind(str, Enum):
    """One hop in a traceable evidence chain."""

    ROUTE = "ROUTE"
    CALL = "CALL"
    SITE = "SITE"
    DATA_FLOW = "DATA_FLOW"
    COVERAGE = "COVERAGE"


class FactorCategory(str, Enum):
    """Why something other than the root cause also matters."""

    MISSING_COVERAGE = "MISSING_COVERAGE"
    UNEXERCISED_ENTRY_POINT = "UNEXERCISED_ENTRY_POINT"
    PARTIAL_INVESTIGATION = "PARTIAL_INVESTIGATION"
    UNRESOLVED_CONFLICT = "UNRESOLVED_CONFLICT"


# ── Traceable references ──────────────────────────────────────────────────────


class EvidenceRef(BaseModel):
    """A pointer back to one stored evidence item.

    Only the identifying and locating fields are copied.  The full item stays in
    the run's evidence store so there is a single source of truth for it.
    """

    evidence_id: str
    agent: str
    evidence_type: EvidenceType
    title: str
    source_file: str | None = None
    source_line: int | None = None
    #: The Phase 7 analysis method that produced the item (e.g. ``ast_call_graph``).
    method: str | None = None


class SourceAnchor(BaseModel):
    """A concrete location in the analysed repository."""

    file_path: str
    line: int | None = None
    line_end: int | None = None
    function: str | None = None
    #: The source text at the anchor, when the producing evidence carried it.
    excerpt: str | None = None


class EvidenceChainLink(BaseModel):
    """One hop of the path from the reported entry point to the defect site."""

    kind: ChainNodeKind
    label: str
    anchor: SourceAnchor | None = None
    #: Stable evidence IDs justifying this hop.  Never empty.
    evidence_ids: list[str] = Field(default_factory=list)
    detail: str = ""


# ── Findings ──────────────────────────────────────────────────────────────────


class RootCauseCandidate(BaseModel):
    """A possible root cause considered by the synthesizer.

    Candidates are produced by deterministic rules; they are not guesses.  The
    ``support_score`` exists purely for deterministic ranking and is documented in
    ``app.analysis.evidence_synthesizer``.
    """

    kind: RootCauseKind
    summary: str
    rationale: str
    anchor: SourceAnchor
    evidence_ids: list[str] = Field(default_factory=list)
    support_score: float = 0.0


class ContributingFactor(BaseModel):
    """A real condition that is not itself the root cause but matters."""

    category: FactorCategory
    description: str
    evidence_ids: list[str] = Field(default_factory=list)


class ConflictingEvidence(BaseModel):
    """Two evidence items that cannot both be right.

    Conflicts are surfaced rather than silently resolved.  A conflict lowers
    confidence and becomes an unresolved question; it never gets averaged away.
    """

    description: str
    evidence_ids: list[str] = Field(default_factory=list)


# ── The synthesis index ───────────────────────────────────────────────────────
#
# The intermediate product between the two Phase 8 stages.  ``EVIDENCE_PENDING``
# normalises, deduplicates, indexes and conflict-checks the Phase 7 evidence;
# ``ROOT_CAUSE_ANALYSIS`` ranks candidates and scores confidence from it.  The
# index is persisted in ``run.context`` so the second stage does not have to
# re-derive it, and so a reviewer can inspect exactly what was reasoned over.


class RouteFactModel(BaseModel):
    """A route Phase 7 resolved statically."""

    http_method: str
    path: str
    handler: str
    file_path: str
    line: int | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class CallEdgeModel(BaseModel):
    """A statically resolved call site.

    ``symbol`` is the bare function name while ``callee`` is the qualified call
    text.  Resolution keys off ``symbol``, because two modules may define a
    function of the same name.
    """

    caller_file: str
    caller_line: int | None = None
    callee: str
    symbol: str
    target_file: str
    target_line: int | None = None
    evidence_id: str


class NumericSiteModel(BaseModel):
    """One arithmetic site recorded by Phase 7's text scanner."""

    file_path: str
    function: str
    line: int
    kind: str
    detail: str = ""
    text: str = ""
    evidence_id: str


class DataFlowModel(BaseModel):
    """An attribute observed to be written in one file and read in another."""

    attribute: str
    written_in: list[str] = Field(default_factory=list)
    read_in: list[str] = Field(default_factory=list)
    evidence_id: str


class CoverageGap(BaseModel):
    """A pair of modules Phase 7 reported as never tested together."""

    concept: str
    first_module: str
    second_module: str


class SynthesisFinding(BaseModel):
    """Identity of one deduplicated finding.

    Only the identifying fields are kept: the full item remains in the run's
    evidence store, and duplicating it here would create two sources of truth.
    """

    evidence_id: str
    agent: str
    evidence_type: EvidenceType
    title: str
    source_file: str | None = None
    source_line: int | None = None
    method: str | None = None
    #: Distinct agents that reported the same finding.  More than one means the
    #: finding is corroborated by independent investigators.
    corroborating_agents: list[str] = Field(default_factory=list)


class EvidenceSynthesis(BaseModel):
    """The serialisable output of the ``EVIDENCE_PENDING`` stage.

    ``SynthesisConfig`` is recorded so a reviewer knows which bounds produced the
    index, and ``rejected_types`` names the evidence types that were refused so
    the exclusion is auditable rather than invisible.
    """

    raw_evidence_count: int
    finding_count: int
    evidence_counts: dict[str, int] = Field(default_factory=dict)
    findings: list[SynthesisFinding] = Field(default_factory=list)
    routes: list[RouteFactModel] = Field(default_factory=list)
    call_edges: list[CallEdgeModel] = Field(default_factory=list)
    numeric_sites: list[NumericSiteModel] = Field(default_factory=list)
    data_flows: list[DataFlowModel] = Field(default_factory=list)
    coverage_gaps: list[CoverageGap] = Field(default_factory=list)
    untested_route_files: list[str] = Field(default_factory=list)
    covered_files: list[str] = Field(default_factory=list)
    conflicts: list[ConflictingEvidence] = Field(default_factory=list)
    #: Evidence types that were present but refused, e.g. ``VERIFIED_RESULT``.
    rejected_types: list[str] = Field(default_factory=list)
    rejected_count: int = 0


class UnresolvedQuestion(BaseModel):
    """Something Phase 8 could not answer from static evidence."""

    question: str
    why_it_matters: str
    #: What would settle the question.  Phrased as an investigation step, never
    #: as a fix — Phase 9 owns fixes.
    recommended_step: str
    evidence_ids: list[str] = Field(default_factory=list)


class ConfidenceBreakdown(BaseModel):
    """Explainable provenance for the confidence score.

    A single opaque number would invite clients to treat it as a probability.
    Exposing the base, the corroboration bonus and every named penalty keeps the
    number auditable.
    """

    base: float
    corroboration_bonus: float
    penalties: dict[str, float] = Field(default_factory=dict)
    score: float = 0.0
    level: ConfidenceLevel = ConfidenceLevel.LOW
    independent_investigators: int = 0
    direct_observations: int = 0
    inferred_hypotheses: int = 0
    rationale: str = ""


# ── The analysis ──────────────────────────────────────────────────────────────


class RootCauseAnalysis(BaseModel):
    """Phase 8's verdict for one run.

    ``root_cause`` is ``None`` unless ``status`` is ``DETERMINED`` or
    ``PARTIAL`` *and* a candidate was found, so a client cannot read a summary
    field while ignoring the status that qualifies it.
    """

    run_id: str
    issue: str
    status: RootCauseStatus

    #: One-paragraph conclusion.  ``None`` when no root cause was determined.
    root_cause: str | None = None
    root_cause_kind: RootCauseKind | None = None
    #: Where in the repository the conclusion points.
    anchor: SourceAnchor | None = None

    confidence: float = 0.0
    confidence_level: ConfidenceLevel = ConfidenceLevel.LOW
    confidence_rationale: str = ""
    confidence_breakdown: ConfidenceBreakdown

    #: Traceable path from the reported entry point to the conclusion.
    evidence_chain: list[EvidenceChainLink] = Field(default_factory=list)
    #: Evidence that directly supports the conclusion, strongest first.
    supporting_evidence: list[EvidenceRef] = Field(default_factory=list)
    #: Other candidates that were considered and ranked lower.
    rejected_candidates: list[RootCauseCandidate] = Field(default_factory=list)
    contributing_factors: list[ContributingFactor] = Field(default_factory=list)
    conflicting_evidence: list[ConflictingEvidence] = Field(default_factory=list)
    unresolved_questions: list[UnresolvedQuestion] = Field(default_factory=list)

    #: Provenance of the investigation this verdict is based on.
    investigation_status: InvestigationStatus = InvestigationStatus.PENDING
    partial_investigation: bool = False
    #: Names of investigators that failed, or ``[]``.
    missing_evidence: list[str] = Field(default_factory=list)
    #: Counts per evidence type actually consumed, after deduplication.
    evidence_counts: dict[str, int] = Field(default_factory=dict)
    #: How many raw items collapsed into ``supporting_evidence``' distinct findings.
    raw_evidence_count: int = 0
    deduplicated_evidence_count: int = 0
    #: Always False in Phase 8.  Exposed so a client cannot mistake a static
    #: conclusion for a reproduced defect.
    tests_executed: bool = False
    #: ``VERIFIED_RESULT`` items found on input.  Always 0 in practice: the
    #: synthesizer rejects that type, and Phase 7 cannot emit it.
    verified_result_count: int = 0
    #: How many independent investigators corroborated the conclusion.
    corroborating_investigators: list[str] = Field(default_factory=list)

    #: Ordered, de-duplicated investigation steps that would raise confidence.
    recommended_next_investigation: list[str] = Field(default_factory=list)

    # ── Persistence helpers ───────────────────────────────────────────────
    #
    # The analysis is stored in ``MentorRun.context`` rather than as a second
    # evidence list.  It holds only *references* (stable evidence IDs), so
    # duplicating evidence here would create two sources of truth.

    def to_summary(self) -> dict[str, Any]:
        """Serialise the analysis for ``run.context``.

        The model is timestamp-free by design, so this is a total, lossless
        representation and re-reading it yields an identical object.
        """
        return self.model_dump(mode="json")

    @classmethod
    def from_summary(
        cls, run_id: str, summary: dict[str, Any]
    ) -> "RootCauseAnalysis":
        """Rebuild an analysis previously written by :meth:`to_summary`."""
        data = dict(summary)
        data["run_id"] = run_id
        return cls.model_validate(data)

    def is_determined(self) -> bool:
        """True when a root cause was actually identified."""
        return self.root_cause is not None and self.status in (
            RootCauseStatus.DETERMINED,
            RootCauseStatus.PARTIAL,
        )


#: Evidence types the synthesizer is willing to reason from.  ``VERIFIED_RESULT``
#: is excluded on purpose: Phase 8 must not be handed a verified result, because
#: accepting one would let a stale or mislabelled item silently inflate
#: confidence.  Phase 7 cannot emit one either.
PHASE8_CONSUMED_EVIDENCE_TYPES: frozenset[EvidenceType] = frozenset(
    {
        EvidenceType.OBSERVATION,
        EvidenceType.HYPOTHESIS,
        EvidenceType.PROPOSED_ACTION,
    }
)

__all__ = [
    "HIGH_CONFIDENCE_THRESHOLD",
    "MEDIUM_CONFIDENCE_THRESHOLD",
    "PHASE8_CONSUMED_EVIDENCE_TYPES",
    "CallEdgeModel",
    "ChainNodeKind",
    "ConfidenceBreakdown",
    "ConfidenceLevel",
    "ContributingFactor",
    "ConflictingEvidence",
    "CoverageGap",
    "DataFlowModel",
    "EvidenceChainLink",
    "EvidenceRef",
    "EvidenceSynthesis",
    "FactorCategory",
    "NumericSiteModel",
    "RouteFactModel",
    "RootCauseAnalysis",
    "RootCauseCandidate",
    "RootCauseKind",
    "RootCauseStatus",
    "SourceAnchor",
    "SynthesisFinding",
    "UnresolvedQuestion",
]
