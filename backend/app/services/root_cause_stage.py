"""
Evidence synthesis and root-cause analysis — Phase 8.

Two stages, one per workflow state, split along the seam the evidence types
already imply:

``EVIDENCE_PENDING``
    :class:`EvidenceSynthesisStage` — normalise, deduplicate, index the Phase 7
    evidence and detect conflicts.  It makes **no** judgement about what is wrong
    with the repository, and it never returns ``StageOutcome.PENDING``: the work
    is real, even when the result is "nothing to reason from".

``ROOT_CAUSE_ANALYSIS``
    :class:`RootCauseAnalysisStage` — rank candidate root causes over the stored
    index, score confidence with an auditable breakdown, and build the traceable
    evidence chain.  This is the first stage in the system allowed to name a root
    cause.

The run then stops at ``ROOT_CAUSE_READY``, where the Phase 9 placeholder keeps it
parked.  Proposing a fix is Phase 9's job, and stopping here is what keeps the two
honestly separated.

Why two stages rather than one
------------------------------
Each stage has one job and one failure mode, so each emits its own events and can
be retried or inspected on its own.  It also means the intermediate synthesis is
persisted and reviewable: a reviewer can see exactly which findings, call edges
and coverage gaps the conclusion was drawn from, without re-running anything.

Honesty boundaries
------------------
* Read-only.  Neither stage opens, executes, imports or writes a repository file.
  Every fact comes from the evidence Phase 7 already recorded.
* No root cause without citations.  Every claim in a :class:`RootCauseAnalysis`
  carries stable ``Evidence.id`` values pointing at the source lines behind it.
* No verification.  ``VERIFIED_RESULT`` is not an accepted input type, and the
  emitted model has no field that could carry a test exit code.  A root cause
  here is a lead confirmed by *reading* code, never by *running* it.
* Deterministic.  Neither stage reads a clock, and the analysis model is
  timestamp-free, so identical evidence yields an identical verdict.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.analysis.evidence_synthesizer import (
    DEFAULT_SYNTHESIS_CONFIG,
    EvidenceIndex,
    EvidenceSynthesisError,
    EvidenceSynthesizer,
    SynthesisConfig,
)
from app.models.root_cause import EvidenceSynthesis, RootCauseAnalysis
from app.models.workflow import StageResult, WorkflowState
from app.services.investigation_stage import load_investigation
from app.services.stages import StageContext

logger = logging.getLogger(__name__)

#: ``run.context`` key holding the Phase 8 synthesis index.
EVIDENCE_SYNTHESIS_KEY = "evidence_synthesis"

#: ``run.context`` key holding the Phase 8 analysis.
ROOT_CAUSE_KEY = "root_cause_summary"

#: ``run.context`` key holding when synthesis ran.  Kept out of the analysis
#: model on purpose: a wall-clock value inside the model would make two
#: identical analyses compare unequal, which would break replay and caching.
ROOT_CAUSE_SYNTHESIZED_AT_KEY = "root_cause_synthesized_at"


class EvidenceSynthesisStage:
    """Normalises and indexes Phase 7 evidence. Registered for ``EVIDENCE_PENDING``.

    Consumes only the types in ``PHASE8_CONSUMED_EVIDENCE_TYPES``.  Anything else
    — in particular a ``VERIFIED_RESULT``, which no implemented phase can produce
    — is refused and counted in ``rejected_types`` so the exclusion is visible in
    the API rather than silent.
    """

    state = WorkflowState.EVIDENCE_PENDING

    def __init__(self, config: SynthesisConfig | None = None) -> None:
        self._config = config or DEFAULT_SYNTHESIS_CONFIG

    def execute(self, context: StageContext) -> StageResult:
        run = context.run
        emit = context.emit
        investigation = load_investigation(run)

        if emit is not None:
            emit(
                "EVIDENCE_SYNTHESIS_STARTED",
                (
                    f"Synthesising {len(investigation.evidence)} Phase 7 evidence "
                    f"item(s) from {len(investigation.investigators)} investigator "
                    f"report(s). No repository code is read or executed."
                ),
                {
                    "evidence_count": len(investigation.evidence),
                    "investigation_status": investigation.status.value,
                    "issue": run.issue,
                },
            )

        try:
            synthesis, _index = EvidenceSynthesizer(self._config).synthesize_evidence(
                investigation, issue=run.issue
            )
        except EvidenceSynthesisError as exc:
            return self._fail(emit, run.run_id, f"{type(exc).__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception("Evidence synthesis failed for run %s", run.run_id)
            return self._fail(emit, run.run_id, f"{type(exc).__name__}: {exc}")

        summary = synthesis.model_dump(mode="json")
        message = _synthesis_message(synthesis)

        if emit is not None:
            emit(
                "EVIDENCE_SYNTHESIS_COMPLETED",
                message,
                {
                    "raw_evidence_count": synthesis.raw_evidence_count,
                    "finding_count": synthesis.finding_count,
                    "evidence_counts": dict(synthesis.evidence_counts),
                    "route_count": len(synthesis.routes),
                    "call_edge_count": len(synthesis.call_edges),
                    "numeric_site_count": len(synthesis.numeric_sites),
                    "coverage_gap_count": len(synthesis.coverage_gaps),
                    "conflict_count": len(synthesis.conflicts),
                    "rejected_count": synthesis.rejected_count,
                    "rejected_types": list(synthesis.rejected_types),
                },
            )

        return StageResult.completed(
            message,
            next_state=self.state,
            **{
                EVIDENCE_SYNTHESIS_KEY: summary,
                "evidence_synthesis_finding_count": synthesis.finding_count,
                "evidence_synthesis_conflict_count": len(synthesis.conflicts),
            },
        )

    def _fail(self, emit, run_id: str, error: str) -> StageResult:
        if emit is not None:
            emit(
                "ROOT_CAUSE_ANALYSIS_FAILED",
                f"Evidence synthesis failed for run {run_id}: {error}",
                {"run_id": run_id, "error": error},
            )
        return StageResult.failed("Evidence synthesis could not be performed.", error)


class RootCauseAnalysisStage:
    """Ranks root-cause candidates and records the verdict. For ``ROOT_CAUSE_ANALYSIS``.

    Reads the synthesis index the previous stage stored.  A missing or unusable
    index is reported as a stage failure rather than treated as "no evidence",
    because a silent fallback here would report a weaker conclusion as though it
    had been reached by the intended path.
    """

    state = WorkflowState.ROOT_CAUSE_ANALYSIS

    def __init__(self, config: SynthesisConfig | None = None) -> None:
        self._config = config or DEFAULT_SYNTHESIS_CONFIG

    def execute(self, context: StageContext) -> StageResult:
        run = context.run
        emit = context.emit
        investigation = load_investigation(run)

        if emit is not None:
            emit(
                "ROOT_CAUSE_ANALYSIS_STARTED",
                (
                    f"Ranking root-cause candidates for run {run.run_id}. "
                    f"Analysis is static: nothing is executed and no test is "
                    f"verified."
                ),
                {
                    "run_id": run.run_id,
                    "investigation_status": investigation.status.value,
                    "evidence_count": len(investigation.evidence),
                },
            )

        synthesis = load_synthesis(run)
        if synthesis is None:
            return self._fail(
                emit,
                run.run_id,
                "No evidence synthesis was recorded for this run. "
                "EVIDENCE_PENDING must run before ROOT_CAUSE_ANALYSIS.",
            )

        by_id = {item.id: item for item in investigation.evidence}
        index = EvidenceIndex.from_synthesis(synthesis, by_id)

        try:
            analysis = EvidenceSynthesizer(self._config).analyze(
                investigation, index, issue=run.issue
            )
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception("Root-cause analysis failed for run %s", run.run_id)
            return self._fail(emit, run.run_id, f"{type(exc).__name__}: {exc}")

        summary = analysis.to_summary()
        message = _analysis_message(analysis)
        # Reference only.  Deliberately not part of the analysis model.
        synthesized_at = _utc_now_iso()

        if emit is not None:
            emit(
                "ROOT_CAUSE_ANALYSIS_COMPLETED",
                message,
                {
                    "run_id": run.run_id,
                    "status": analysis.status.value,
                    "root_cause_kind": (
                        analysis.root_cause_kind.value
                        if analysis.root_cause_kind
                        else None
                    ),
                    "confidence": analysis.confidence,
                    "confidence_level": analysis.confidence_level.value,
                    "supporting_evidence_count": len(analysis.supporting_evidence),
                    "chain_length": len(analysis.evidence_chain),
                    "conflict_count": len(analysis.conflicting_evidence),
                    "unresolved_question_count": len(analysis.unresolved_questions),
                    "partial_investigation": analysis.partial_investigation,
                    "tests_executed": analysis.tests_executed,
                },
            )

        return StageResult.completed(
            message,
            next_state=self.state,
            **{
                ROOT_CAUSE_KEY: summary,
                ROOT_CAUSE_SYNTHESIZED_AT_KEY: synthesized_at,
                "root_cause_status": analysis.status.value,
                "root_cause_confidence": analysis.confidence,
                "root_cause_confidence_level": analysis.confidence_level.value,
            },
        )

    def _fail(self, emit, run_id: str, error: str) -> StageResult:
        if emit is not None:
            emit(
                "ROOT_CAUSE_ANALYSIS_FAILED",
                f"Root-cause analysis failed for run {run_id}: {error}",
                {"run_id": run_id, "error": error},
            )
        return StageResult.failed("Root-cause analysis could not be performed.", error)


# ── Persistence helpers ───────────────────────────────────────────────────────


def load_synthesis(run) -> EvidenceSynthesis | None:
    """Return the synthesis stored for ``run``, or None if absent/unparseable.

    A malformed synthesis is reported as ``None`` so the caller can fail the
    stage with a clear reason instead of raising a 500 from a read-only endpoint.
    """
    raw = run.context.get(EVIDENCE_SYNTHESIS_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return EvidenceSynthesis.model_validate(raw)
    except Exception as exc:  # noqa: BLE001 - a bad summary must not crash a read
        logger.warning(
            "Evidence synthesis for run %s could not be parsed: %s", run.run_id, exc
        )
        return None


def load_root_cause(run) -> RootCauseAnalysis | None:
    """Return the analysis stored for ``run``, or None if absent/unparseable.

    Used by the read-only API so that a corrupt context is reported as "not
    available" rather than raising.
    """
    raw = run.context.get(ROOT_CAUSE_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return RootCauseAnalysis.from_summary(run.run_id, raw)
    except Exception as exc:  # noqa: BLE001 - a bad summary must not crash a read
        logger.warning(
            "Root-cause analysis for run %s could not be parsed: %s", run.run_id, exc
        )
        return None


# ── Message helpers ───────────────────────────────────────────────────────────


def _synthesis_message(synthesis: EvidenceSynthesis) -> str:
    parts = [
        f"Synthesis complete: {synthesis.finding_count} distinct finding(s) from "
        f"{synthesis.raw_evidence_count} evidence item(s)"
    ]
    if synthesis.conflicts:
        parts.append(
            f"{len(synthesis.conflicts)} conflicting group(s) detected"
        )
    if synthesis.rejected_count:
        parts.append(
            f"{synthesis.rejected_count} item(s) of type "
            f"{', '.join(synthesis.rejected_types)} refused"
        )
    if synthesis.finding_count == 0:
        parts.append(
            "no evidence was usable, so no root cause can be proposed"
        )
    return "; ".join(parts) + "."


def _analysis_message(analysis: RootCauseAnalysis) -> str:
    if analysis.status.value == "NO_EVIDENCE" or analysis.root_cause is None:
        return (
            f"No root cause was determined: {analysis.status.value}. "
            f"{len(analysis.unresolved_questions)} unresolved question(s) "
            f"recorded. Nothing was executed, so nothing is verified."
        )
    partial = " on partial evidence" if analysis.partial_investigation else ""
    return (
        f"Root cause identified{partial} with {analysis.confidence_level.value} "
        f"confidence ({analysis.confidence}): {analysis.root_cause} "
        f"Supported by {len(analysis.supporting_evidence)} evidence item(s). "
        f"Not verified: no test was executed."
    )


def _utc_now_iso() -> str:
    """Return the current UTC time as an ISO string, for reference only."""
    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "EVIDENCE_SYNTHESIS_KEY",
    "ROOT_CAUSE_KEY",
    "ROOT_CAUSE_SYNTHESIZED_AT_KEY",
    "EvidenceSynthesisStage",
    "RootCauseAnalysisStage",
    "load_root_cause",
    "load_synthesis",
]
