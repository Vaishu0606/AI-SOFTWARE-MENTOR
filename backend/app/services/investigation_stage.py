"""
Investigation stage — Phase 7.

Replaces the ``INVESTIGATION_PENDING`` placeholder with a real stage that runs
the three investigators concurrently and returns their evidence.

What this stage does
--------------------
1. builds one read-only :class:`~app.investigation.context.InvestigationContext`
   from the Phase 3/4/5 analyses,
2. runs :class:`~app.investigation.runner.InvestigationRunner`, which executes
   the three investigators on separate threads,
3. emits an event per investigator transition through the ``StageContext`` emitter,
4. returns every evidence item plus a summary in ``StageResult.data``.

Honesty boundaries
------------------
This stage never declares a root cause.  It returns ``COMPLETED`` and advances
to ``EVIDENCE_PENDING`` on success, which is exactly the point at which Phase 8
is supposed to take over — synthesis and root-cause ranking are Phase 8's job,
and stopping here is what keeps the two phases honestly separated.

Partial results are preserved, not hidden: if one investigator fails, the run
still advances with the other two investigators' evidence and a ``PARTIAL``
status recorded in the summary, the events, and ``result.data``.  Silently
reporting two-thirds of an investigation as a complete one would be worse than
failing the run.
"""

from __future__ import annotations

import logging

from app.investigation.context import InvestigationContext
from app.investigation.runner import InvestigationRunner
from app.models.investigation import (
    InvestigationConfig,
    InvestigationResult,
    InvestigationStatus,
    InvestigatorName,
    InvestigatorStatus,
)
from app.models.workflow import MentorRun, StageOutcome, StageResult, WorkflowState
from app.services.stages import StageContext

logger = logging.getLogger(__name__)


class InvestigationStage:
    """Runs parallel bug investigation for a run in ``INVESTIGATION_PENDING``."""

    state = WorkflowState.INVESTIGATION_PENDING

    def __init__(self, config: InvestigationConfig | None = None) -> None:
        self._config = config or InvestigationConfig()

    def execute(self, context: StageContext) -> StageResult:
        run = context.run
        emit = context.emit

        try:
            investigation_context = InvestigationContext.from_run(
                run_id=run.run_id,
                issue=run.issue,
                repository_path=run.repository_path,
                config=self._config,
            )
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception(
                "InvestigationContext could not be built for run %s", run.run_id
            )
            return StageResult.failed(
                "Bug investigation could not start.",
                f"{type(exc).__name__}: {exc}",
            )

        snapshot = investigation_context.snapshot
        if emit is not None:
            emit(
                "INVESTIGATION_STARTED",
                (
                    f"Investigating {snapshot.name}: {snapshot.file_count} file(s), "
                    f"{len(snapshot.ast.all_functions)} function(s), "
                    f"{len(snapshot.ast.routes)} route(s) analysed by Phases 3-5. "
                    f"Three investigators will run concurrently."
                ),
                {
                    "repository_name": snapshot.name,
                    "file_count": snapshot.file_count,
                    "function_count": len(snapshot.ast.all_functions),
                    "route_count": len(snapshot.ast.routes),
                    "investigators": [n.value for n in InvestigatorName],
                },
            )

        runner = InvestigationRunner(
            investigation_context, on_progress=_make_progress_hook(emit)
        )
        result = runner.run()

        if emit is not None:
            # Per-investigator start/complete/failure events were already emitted
            # live by the progress hook; only the aggregate remains.
            emit(
                "INVESTIGATION_COMPLETED",
                _completion_message(result),
                {
                    "status": result.status.value,
                    "evidence_count": len(result.evidence),
                    "errors": list(result.errors),
                    "investigators": [
                        {
                            "name": r.investigator.value,
                            "status": r.status.value,
                            "evidence_count": r.evidence_count,
                        }
                        for r in result.investigators
                    ],
                },
            )

        outcome, message = _settle(result, snapshot.name)

        if outcome is StageOutcome.FAILED:
            return StageResult.failed(message, "; ".join(result.errors) or "unknown")

        return StageResult.completed(
            message,
            next_state=self.state,
            evidence=result.evidence,
            investigation_summary=result.to_summary(),
            investigation_status=result.status.value,
            evidence_count=len(result.evidence),
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_progress_hook(emit):
    """Adapt the stage's emitter into the runner's per-investigator hook.

    ``emit`` may be None (a stage invoked directly in a test), in which case no
    events are produced and the hook is a no-op.  Completion is reported here
    rather than after ``run()`` returns, so a listener sees each investigator
    finish as it happens rather than all at the end.
    """
    if emit is None:
        return None

    def hook(
        name: InvestigatorName, status: InvestigatorStatus, detail: dict
    ) -> None:
        if status is InvestigatorStatus.RUNNING:
            emit(
                "INVESTIGATOR_STARTED",
                f"{name.label} started.",
                {"investigator": name.value},
            )
        elif status is InvestigatorStatus.COMPLETED:
            emit(
                "INVESTIGATOR_COMPLETED",
                f"{name.label} completed with {detail.get('evidence_count', 0)} "
                f"evidence item(s) in {detail.get('duration_seconds', 0.0):.3f}s.",
                {
                    "investigator": name.value,
                    "evidence_count": detail.get("evidence_count", 0),
                    "duration_seconds": detail.get("duration_seconds", 0.0),
                },
            )
        else:
            emit(
                "INVESTIGATOR_FAILED",
                f"{name.label} failed: {detail.get('error') or 'unknown error'}. "
                f"Evidence from the other investigators is unaffected.",
                {
                    "investigator": name.value,
                    "error": detail.get("error"),
                },
            )

    return hook


def _completion_message(result: InvestigationResult) -> str:
    if result.status is InvestigationStatus.COMPLETED:
        return (
            f"Investigation completed: {len(result.evidence)} evidence item(s) from "
            f"{len(result.completed_investigators)} investigator(s). "
            f"No root cause is claimed; synthesis is Phase 8."
        )
    if result.status is InvestigationStatus.PARTIAL:
        return (
            f"Investigation partially completed: {len(result.evidence)} evidence "
            f"item(s) from {len(result.completed_investigators)} investigator(s), "
            f"{len(result.failed_investigators)} failed. "
            f"No root cause is claimed; synthesis is Phase 8."
        )
    return (
        f"Investigation produced no evidence: all "
        f"{len(result.investigators)} investigator(s) failed. "
        f"No root cause is claimed; synthesis is Phase 8."
    )


def _settle(result: InvestigationResult, repository_name: str) -> tuple[StageOutcome, str]:
    """Map the investigation status onto a stage outcome and message."""
    if result.status is InvestigationStatus.COMPLETED:
        return (
            StageOutcome.COMPLETED,
            _completion_message(result)
            + f" Repository: {repository_name}.",
        )
    if result.status is InvestigationStatus.PARTIAL:
        return (
            StageOutcome.COMPLETED,
            _completion_message(result)
            + f" Repository: {repository_name}. Failing investigators: "
            + "; ".join(result.errors),
        )
    return (
        StageOutcome.FAILED,
        _completion_message(result) + f" Repository: {repository_name}.",
    )


# ── Persistence helper ────────────────────────────────────────────────────────

#: ``run.context`` key holding the serialised investigation summary.  The
#: evidence list itself is *not* stored here: it already lives on
#: ``MentorRun.evidence``, and duplicating it would create two sources of truth.
INVESTIGATION_SUMMARY_KEY = "investigation_summary"

#: Phase 7 stamps every evidence item it produces with this value in
#: ``metadata["phase"]``, so later phases can select their own output.
INVESTIGATION_EVIDENCE_PHASE = 7


def load_investigation(run: MentorRun) -> InvestigationResult:
    """Rebuild the investigation stored for ``run``.

    Shared by the Phase 7 read API and the Phase 8 analysis stage so that both
    see exactly the same result; a second implementation would be free to drift
    and report a different investigation than the one that was analysed.

    A run that never reached the investigation stage yields ``PENDING`` with no
    evidence, which is the honest representation of "not investigated yet".  A
    stored summary that cannot be parsed yields ``FAILED`` with an error rather
    than raising, so a corrupt context degrades one endpoint's answer instead of
    breaking the run.
    """
    summary = run.context.get(INVESTIGATION_SUMMARY_KEY)
    if not isinstance(summary, dict):
        return InvestigationResult(run_id=run.run_id)

    evidence = [
        item
        for item in run.evidence
        if item.metadata.get("phase") == INVESTIGATION_EVIDENCE_PHASE
    ]
    try:
        return InvestigationResult.from_summary(run.run_id, summary, evidence)
    except (ValueError, KeyError, TypeError):
        logger.warning("Investigation summary for run %s could not be parsed", run.run_id)
        return InvestigationResult(
            run_id=run.run_id,
            status=InvestigationStatus.FAILED,
            errors=["Stored investigation summary could not be parsed."],
        )


__all__ = [
    "INVESTIGATION_EVIDENCE_PHASE",
    "INVESTIGATION_SUMMARY_KEY",
    "InvestigationStage",
    "load_investigation",
]
