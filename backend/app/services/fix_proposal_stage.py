"""
Fix proposal — Phase 9.

Two stages, at the two states Phase 9 owns:

``ROOT_CAUSE_READY``
    :class:`RootCauseReadyStage` — the hand-off from Phase 8.  It asserts that
    the analysis Phase 8 was supposed to record is actually present and readable,
    and refuses to continue otherwise.  It deliberately proposes nothing: the
    whole point of the state is to make "the root cause is on the record" an
    explicit, checkable precondition rather than an assumption.

``FIX_PROPOSAL``
    :class:`FixProposalStage` — run the deterministic proposer over the Phase 8
    analysis and record the resulting proposal.  This is the first stage in the
    system allowed to *read* a repository file, and only through the constrained
    reader in ``app.analysis.fix_proposer``.  It still writes nothing.

The run then stops at ``FIX_PROPOSAL_READY``, where the placeholder keeps it
parked.  Phase 9 ends at a human approval gate by design: the rule that a fix
is never applied without explicit approval is only meaningful if the workflow
physically stops where approval would be given.

Why the gate is a stage and not a check
---------------------------------------
Without ``RootCauseReadyStage`` a run would enter ``ROOT_CAUSE_READY`` and stall
on the placeholder that used to live there, so the proposal stage would never
run.  Making the hand-off a real stage keeps the "one stage, one state" contract
intact and turns a missing Phase 8 result into a loud failure instead of a
silent skip.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.analysis.fix_proposer import DEFAULT_FIX_PROPOSER, FixProposer
from app.ingestion.cloner import IngestionError, LocalSource, RepositoryCloner
from app.models.fix_proposal import FixProposal, FixProposalStatus
from app.models.workflow import StageResult, WorkflowState
from app.services.root_cause_stage import load_root_cause
from app.services.stages import StageContext

logger = logging.getLogger(__name__)

#: ``run.context`` key holding the Phase 9 proposal.
FIX_PROPOSAL_KEY = "fix_proposal"

#: ``run.context`` key holding when the proposal was drafted.  Kept out of the
#: model on exactly the Phase 8 reasoning: a wall-clock value inside the model
#: would make two identical proposals compare unequal, breaking replay.
FIX_PROPOSAL_CREATED_AT_KEY = "fix_proposal_created_at"


class RootCauseReadyStage:
    """Validates the Phase 8 hand-off. Registered for ``ROOT_CAUSE_READY``.

    A missing analysis is a stage failure rather than an "insufficient evidence"
    proposal: reaching this state without one means the workflow was driven out
    of order, and reporting that as a considered opinion about the repository
    would misrepresent a wiring fault as a finding.
    """

    state = WorkflowState.ROOT_CAUSE_READY

    def execute(self, context: StageContext) -> StageResult:
        analysis = load_root_cause(context.run)
        if analysis is None:
            return StageResult.failed(
                "Phase 9 cannot start: no root-cause analysis is recorded.",
                "ROOT_CAUSE_ANALYSIS must record an analysis before a fix can be "
                "proposed from it.",
            )

        return StageResult.completed(
            (
                f"Root cause on record: {analysis.status.value} at "
                f"{analysis.confidence_level.value} confidence "
                f"({analysis.confidence}). Handing off to fix proposal."
            ),
            next_state=self.state,
            **{
                "root_cause_handoff_status": analysis.status.value,
                "root_cause_determined": analysis.is_determined(),
            },
        )


class FixProposalStage:
    """Drafts a fix proposal and diff. Registered for ``FIX_PROPOSAL``.

    An honest "I cannot responsibly change this" is a *completed* stage with an
    ``INSUFFICIENT_EVIDENCE`` or ``INVALID`` proposal, not a failure — the run
    still has to reach the approval gate so the developer can see why nothing was
    proposed.  Only an unexpected error fails the run.
    """

    state = WorkflowState.FIX_PROPOSAL

    def __init__(
        self, proposer: FixProposer | None = None, cloner: RepositoryCloner | None = None
    ) -> None:
        self._proposer = proposer or DEFAULT_FIX_PROPOSER
        self._cloner = cloner or RepositoryCloner()

    def execute(self, context: StageContext) -> StageResult:
        run = context.run
        emit = context.emit

        if emit is not None:
            emit(
                "FIX_PROPOSAL_STARTED",
                (
                    f"Drafting a fix proposal for run {run.run_id}. The change "
                    f"and its diff are built in memory only: no file in the "
                    f"repository is written and no test is executed."
                ),
                {"run_id": run.run_id, "issue": run.issue},
            )

        analysis = load_root_cause(run)
        if analysis is None:
            return self._fail(
                emit,
                run.run_id,
                "No root-cause analysis is recorded for this run. "
                "ROOT_CAUSE_ANALYSIS must run before FIX_PROPOSAL.",
            )

        try:
            root = self._cloner.prepare(LocalSource(path=run.repository_path))
        except IngestionError as exc:
            return self._fail(emit, run.run_id, exc.reason)
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception("Fix proposal could not resolve the repository")
            return self._fail(emit, run.run_id, f"{type(exc).__name__}: {exc}")

        try:
            proposal = self._proposer.propose(analysis, root)
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception("Fix proposal failed for run %s", run.run_id)
            return self._fail(emit, run.run_id, f"{type(exc).__name__}: {exc}")

        # Reference only.  Deliberately not part of the proposal model.
        created_at = datetime.now(timezone.utc).isoformat()
        message = _proposal_message(proposal)

        if emit is not None:
            emit(
                "FIX_PROPOSAL_COMPLETED",
                message,
                {
                    "run_id": run.run_id,
                    "status": proposal.status.value,
                    "root_cause_kind": (
                        proposal.root_cause_kind.value
                        if proposal.root_cause_kind
                        else None
                    ),
                    "root_cause_confidence_level": (
                        proposal.root_cause_confidence_level.value
                    ),
                    "change_count": len(proposal.changes),
                    "added_lines": proposal.added_line_count(),
                    "removed_lines": proposal.removed_line_count(),
                    "risk_level": proposal.risk.level.value,
                    "affected_files": list(proposal.affected_files),
                    "tests_executed": proposal.tests_executed,
                    "applied": False,
                },
            )

        return StageResult.completed(
            message,
            next_state=self.state,
            **{
                FIX_PROPOSAL_KEY: proposal.to_summary(),
                FIX_PROPOSAL_CREATED_AT_KEY: created_at,
                "fix_proposal_status": proposal.status.value,
                "fix_proposal_change_count": len(proposal.changes),
                "fix_proposal_risk_level": proposal.risk.level.value,
                "fix_proposal_tests_executed": proposal.tests_executed,
            },
        )

    def _fail(self, emit, run_id: str, error: str) -> StageResult:
        if emit is not None:
            emit(
                "FIX_PROPOSAL_FAILED",
                f"Fix proposal failed for run {run_id}: {error}",
                {"run_id": run_id, "error": error},
            )
        return StageResult.failed("A fix proposal could not be produced.", error)


# ── Persistence helpers ───────────────────────────────────────────────────────


def load_fix_proposal(run) -> FixProposal | None:
    """Return the proposal stored for ``run``, or None if absent/unparseable.

    A malformed proposal is reported as ``None`` so a read-only endpoint can say
    "not available" instead of raising a 500 on a client that only wants to look.
    """
    raw = run.context.get(FIX_PROPOSAL_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return FixProposal.from_summary(run.run_id, raw)
    except Exception as exc:  # noqa: BLE001 - a bad summary must not crash a read
        logger.warning("Fix proposal for run %s could not be parsed: %s", run.run_id, exc)
        return None


# ── Message helpers ───────────────────────────────────────────────────────────


def _proposal_message(proposal: FixProposal) -> str:
    if proposal.status is FixProposalStatus.READY_FOR_REVIEW:
        return (
            f"Fix proposal drafted for {', '.join(proposal.affected_files)}: "
            f"{len(proposal.changes)} change(s), "
            f"+{proposal.added_line_count()}/-{proposal.removed_line_count()} "
            f"lines, {proposal.risk.level.value} risk. The diff is in memory "
            f"only — no file was modified and no test was executed. It is ready "
            f"for developer review."
        )
    return (
        f"No fix proposed ({proposal.status.value}). {proposal.rationale} "
        f"Nothing was modified and no test was executed."
    )


__all__ = [
    "FIX_PROPOSAL_CREATED_AT_KEY",
    "FIX_PROPOSAL_KEY",
    "FixProposalStage",
    "RootCauseReadyStage",
    "load_fix_proposal",
]
