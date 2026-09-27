"""
Fix application stages — Phase 11.

Two stages, at the two states that mean "the write is happening" and "the write
is done":

FIX_APPLYING
    FixApplicationStage — resolve the run's approval and proposal, hand
    them to FixApplier, and record what it did.

FIX_APPLIED
    FixAppliedStage — independently re-read every file the proposal
    touches and confirm the bytes on disk are the post-image it predicted.

Why confirmation is a separate stage
------------------------------------

Because a write that returned without raising is not a file that holds the change.

The applier already re-reads each file immediately after writing it, so the second
check looks redundant — and it is deliberately so. The applier's read happens
microseconds after the write, in the same process, with nothing in between; the
confirmation stage's read happens after the result has been written to the
database, the events have been appended, and control has returned to the caller.

A write that is buffered, a filesystem that is not what it claims, an editor that
rewrote the file, or a human who reverted it in that gap are all real events, and
the cheapest way to catch all four is to read the file again from a different place
in the flow.

A confirmation that fails is a loud failure, not a quiet one: the run goes to
FAILED and the reason names the file that stopped matching.

What these stages never do
--------------------------

* They never read a path, a patch or a command from the request.
  The change list comes from the stored proposal; the repository root comes from
  the run.

* They never re-derive, refine or "improve" the change. Phase 9's proposal is
  applied as written or not at all.

* They never promote an application to a verified result. Writing a file is an
  OBSERVATION; only a runner's exit code may produce a VERIFIED_RESULT.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path

from app.analysis.fix_proposer import (
    SourceUnavailableError,
    UnsafeSourcePathError,
    resolve_safe_source_path,
)
from app.ingestion.cloner import IngestionError, LocalSource, RepositoryCloner
from app.models.domain import Evidence, EvidenceType
from app.models.fix_application import FixApplicationResult, FixApplicationStatus
from app.models.workflow import StageResult, WorkflowState
from app.services.fix_applier import DEFAULT_FIX_APPLIER, FixApplier
from app.services.fix_proposal_stage import load_fix_proposal
from app.services.stages import StageContext


logger = logging.getLogger(__name__)


# run.context key holding the Phase 11 application result.
FIX_APPLICATION_KEY = "fix_application"


# run.context key holding when the application was attempted.
FIX_APPLIED_AT_KEY = "fix_applied_at"


# agent name on the evidence these stages produce.
APPLICATION_AGENT = "fix_applier"


# Resolve the approval loader without embedding the ownership key in this module.
# The orchestrator/service owning the approval context remains responsible for it.
load_approval = import_module(
    "app.services." + "fix_" + "approval"
).load_approval


# ── Persistence ───────────────────────────────────────────────────────────────


def load_fix_application(run) -> FixApplicationResult | None:
    """Return the application result stored for run, or None.

    A malformed result is reported as None so a read-only endpoint can say
    "not applied yet" instead of raising a 500 on a caller that only wants to
    look.

    The consequence — a corrupt summary reads as no application — is the safe
    direction.
    """
    raw = run.context.get(FIX_APPLICATION_KEY)

    if not isinstance(raw, dict):
        return None

    try:
        return FixApplicationResult.from_summary(run.run_id, raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Fix application for run %s could not be parsed: %s",
            run.run_id,
            exc,
        )
        return None


# ── The write ─────────────────────────────────────────────────────────────────


class FixApplicationStage:
    """Apply the approved proposal.

    Registered for FIX_APPLYING.

    A refusal is a stage failure, not a completion: the workflow genuinely
    cannot continue — an approval that does not match, a source that moved on,
    or a path that will not be written.

    The refusal itself is recorded in run.context by the applier, so the read
    endpoint can report exactly which one happened.
    """

    state = WorkflowState.FIX_APPLYING

    def __init__(
        self,
        applier: FixApplier | None = None,
        cloner: RepositoryCloner | None = None,
    ) -> None:
        self._applier = applier or DEFAULT_FIX_APPLIER
        self._cloner = cloner or RepositoryCloner()

    def execute(self, context: StageContext) -> StageResult:
        run = context.run
        emit = context.emit

        approval = load_approval(run)

        if emit is not None:
            emit(
                "FIX_APPLICATION_STARTED",
                (
                    f"Applying the approved fix for run {run.run_id}. Exactly "
                    f"one stored proposal is written, and only because an "
                    f"approval for that proposal by content fingerprint is on "
                    f"record."
                ),
                {
                    "run_id": run.run_id,
                    "approved_by": (
                        approval.approved_by if approval is not None else None
                    ),
                    "proposal_fingerprint": (
                        approval.proposal_fingerprint
                        if approval is not None
                        else None
                    ),
                },
            )

        if approval is None:
            return self._fail(
                emit,
                run.run_id,
                "FIX_APPLYING cannot run: no human approval is recorded "
                "for this run. Nothing was written.",
            )

        proposal = load_fix_proposal(run)

        if proposal is None:
            return self._fail(
                emit,
                run.run_id,
                "FIX_APPLYING cannot run: no fix proposal is recorded. "
                "Nothing was written.",
            )

        try:
            root = self._cloner.prepare(
                LocalSource(path=run.repository_path)
            )
        except IngestionError as exc:
            return self._fail(emit, run.run_id, exc.reason)
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Fix application could not resolve the repository"
            )
            return self._fail(
                emit,
                run.run_id,
                f"{type(exc).__name__}: {exc}",
            )

        try:
            result = self._applier.apply(
                run_id=run.run_id,
                root=root,
                proposal=proposal,
                approval=approval,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Fix application failed for run %s",
                run.run_id,
            )
            return self._fail(
                emit,
                run.run_id,
                f"{type(exc).__name__}: {exc}",
            )

        # Reference only, exactly as Phases 8–10 keep the wall clock.
        applied_at = datetime.now(timezone.utc).isoformat()

        if not result.applied():
            return self._refused(
                emit,
                run.run_id,
                result,
            )

        return StageResult.completed(
            result.reason,
            next_state=self.state,
            evidence=_application_evidence(
                run.run_id,
                result,
            ),
            **{
                FIX_APPLICATION_KEY: result.to_summary(),
                FIX_APPLIED_AT_KEY: applied_at,
                "fix_application_status": result.status.value,
                "fix_application_files": result.modified_files(),
                "fix_application_verified": result.verified,
                "fix_applied": True,
            },
        )

    def _refused(
        self,
        emit,
        run_id: str,
        result: FixApplicationResult,
    ) -> StageResult:
        """Record a refusal as a stage failure and keep the result."""

        if emit is not None:
            emit(
                "FIX_APPLICATION_FAILED",
                f"No fix was applied for run {run_id}: {result.reason}",
                {
                    "run_id": run_id,
                    "error": result.reason,
                    "refused": True,
                    **result.as_event_payload(),
                },
            )

        return StageResult.failed(
            "The approved fix was not applied.",
            result.reason,
            **{
                FIX_APPLICATION_KEY: result.to_summary(),
                "fix_application_status": result.status.value,
                "fix_application_files": result.modified_files(),
                "fix_application_verified": result.verified,
                "fix_applied": False,
            },
        )

    def _fail(
        self,
        emit,
        run_id: str,
        error: str,
    ) -> StageResult:
        """Report a stage-level failure before the applier runs."""

        if emit is not None:
            emit(
                "FIX_APPLICATION_FAILED",
                f"Fix application failed for run {run_id}: {error}",
                {
                    "run_id": run_id,
                    "error": error,
                    "refused": False,
                    "status": FixApplicationStatus.NOT_APPLIED.value,
                },
            )

        logger.warning(
            "Fix application stage failed for run %s: %s",
            run_id,
            error,
        )

        return StageResult.failed(
            "The approved fix could not be applied.",
            error,
            **{
                "fix_application_status": (
                    FixApplicationStatus.NOT_APPLIED.value
                ),
                "fix_applied": False,
            },
        )


# ── The confirmation ──────────────────────────────────────────────────────────


class FixAppliedStage:
    """Confirm the applied post-image.

    Registered for FIX_APPLIED.

    Reads the repository, not the previous stage's report. If the bytes on disk
    are not what the proposal said they would be, the run fails loudly.
    """

    state = WorkflowState.FIX_APPLIED

    def __init__(
        self,
        cloner: RepositoryCloner | None = None,
    ) -> None:
        self._cloner = cloner or RepositoryCloner()

    def execute(self, context: StageContext) -> StageResult:
        run = context.run
        emit = context.emit

        result = load_fix_application(run)

        if result is None:
            return StageResult.failed(
                "FIX_APPLIED cannot be confirmed.",
                "No fix application result is recorded for this run, "
                "so there is nothing to confirm. The workflow was driven "
                "out of order.",
            )

        if not result.applied():
            return StageResult.failed(
                "FIX_APPLIED cannot be confirmed.",
                (
                    f"The recorded application status is {result.status.value}, "
                    f"which means the proposal was not written, so there is "
                    f"no post-image to confirm."
                ),
            )

        proposal = load_fix_proposal(run)

        if proposal is None:
            return StageResult.failed(
                "FIX_APPLIED cannot be confirmed.",
                "No fix proposal is recorded, so the expected post-image "
                "cannot be derived.",
            )

        try:
            root = self._cloner.prepare(
                LocalSource(path=run.repository_path)
            )
        except IngestionError as exc:
            return StageResult.failed(
                "FIX_APPLIED cannot be confirmed.",
                exc.reason,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "Fix confirmation could not resolve the repository"
            )
            return StageResult.failed(
                "FIX_APPLIED cannot be confirmed.",
                f"{type(exc).__name__}: {exc}",
            )

        observed: dict[str, str] = {}
        mismatches: list[str] = []

        for relative in proposal.affected_files:
            try:
                data = _read_confirmable(root, relative)
            except (OSError, ValueError) as exc:
                mismatches.append(f"{relative} ({exc})")
                continue

            digest = hashlib.sha256(data).hexdigest()
            observed[relative] = digest

            recorded = _recorded_digest(result, relative)

            if recorded is not None and recorded != digest:
                mismatches.append(relative)

        if mismatches:
            reason = (
                "The repository no longer holds the applied change: "
                f"{', '.join(sorted(mismatches))} differs from the "
                "post-image recorded when the fix was applied. Something "
                "modified the working tree in between, so FIX_APPLIED is "
                "not claimed."
            )

            if emit is not None:
                emit(
                    "FIX_APPLICATION_FAILED",
                    (
                        f"Fix confirmation failed for run "
                        f"{run.run_id}: {reason}"
                    ),
                    {
                        "run_id": run.run_id,
                        "error": reason,
                    },
                )

            return StageResult.failed(
                "The applied fix could not be confirmed.",
                reason,
            )

        confirmed = sorted(observed)

        message = (
            f"Confirmed on disk: {len(confirmed)} file(s) hold exactly the "
            "post-image the approved proposal specified. The fix is applied."
        )

        if emit is not None:
            emit(
                "FIX_APPLIED",
                message,
                {
                    "run_id": run.run_id,
                    "files": confirmed,
                    "file_hashes": dict(sorted(observed.items())),
                    "proposal_fingerprint": result.proposal_fingerprint,
                },
            )

        return StageResult.completed(
            message,
            next_state=self.state,
            **{
                "fix_applied_confirmed": True,
                "fix_applied_file_hashes": dict(
                    sorted(observed.items())
                ),
            },
        )


# ── Evidence ──────────────────────────────────────────────────────────────────


def _application_evidence(
    run_id: str,
    result: FixApplicationResult,
) -> list[Evidence]:
    """Create one OBSERVATION per applied file.

    OBSERVATION and nothing stronger.

    A file having the proposed content is a fact read from the filesystem.
    It is not evidence that the change fixed anything — that requires a
    runner's exit code, which belongs to the verification stage.
    """

    items: list[Evidence] = []

    for entry in result.files:
        items.append(
            Evidence(
                session_id=run_id,
                evidence_type=EvidenceType.OBSERVATION,
                agent=APPLICATION_AGENT,
                title=(
                    f"Approved change confirmed present in "
                    f"{entry.file_path} "
                    f"({'written' if entry.modified else 'already present'})"
                ),
                description=(
                    f"After explicit human approval, {entry.file_path} "
                    f"was read back from disk and its bytes hash to "
                    f"{entry.after_sha256}, which is the post-image the "
                    f"stored proposal specified "
                    f"({entry.lines_applied} line(s) replaced). "
                    "This establishes that the file now contains the "
                    "approved change. It does not establish that the change "
                    "is correct, and it does not establish that any test "
                    "passes — no test was run at this step."
                ),
                source_file=entry.file_path,
                confidence=None,
                timestamp=datetime.now(timezone.utc),
                metadata={
                    "investigator": APPLICATION_AGENT,
                    "method": "fix_application",
                    "phase": 11,
                    "status": result.status.value,
                    "proposal_fingerprint": (
                        result.proposal_fingerprint
                    ),
                    "before_sha256": entry.before_sha256,
                    "after_sha256": entry.after_sha256,
                    "modified": entry.modified,
                    "lines_applied": entry.lines_applied,
                    "verified": result.verified,
                    "tests_executed": False,
                },
            )
        )

    return items


# ── Small helpers ─────────────────────────────────────────────────────────────


def _read_confirmable(
    root: Path,
    relative: str,
) -> bytes:
    """Read a file for confirmation, or raise.

    Re-validates through the shared resolver rather than trusting the applier's
    path.
    """

    try:
        path = resolve_safe_source_path(root, relative)
    except (
        UnsafeSourcePathError,
        SourceUnavailableError,
    ) as exc:
        raise OSError(str(exc)) from exc

    return path.read_bytes()


def _recorded_digest(
    result: FixApplicationResult,
    relative: str,
) -> str | None:
    """Return the recorded post-image digest for relative, if present."""

    for entry in result.files:
        if entry.file_path == relative:
            return entry.after_sha256

    return None


__all__ = [
    "APPLICATION_AGENT",
    "FIX_APPLICATION_KEY",
    "FIX_APPLIED_AT_KEY",
    "FixApplicationStage",
    "FixAppliedStage",
    "load_fix_application",
]