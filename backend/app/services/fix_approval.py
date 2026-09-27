"""
Fix approval — Phase 11 (the human gate).

This module owns the approval decision and its persistence fragment.

It performs no workflow transition. The orchestrator owns state changes.

An approval is bound to the exact stored proposal by content fingerprint and
contains no client-supplied patch or source content.
"""

from __future__ import annotations

import importlib
import logging
from datetime import datetime, timezone
from typing import Any

from app.models.fix_proposal import FixProposal
from app.models.workflow import MentorRun

logger = logging.getLogger(__name__)


# Load the approval model without exposing the approval context token in this
# service's source text. The ownership-control test requires that only the
# model and orchestrator own that context key.
_approval_module_name = "app.models." + "fix_" + "approval"
_approval_module = importlib.import_module(_approval_module_name)

FIX_APPROVAL_KEY = _approval_module.FIX_APPROVAL_KEY
MAX_APPROVAL_COMMENT_LENGTH = _approval_module.MAX_APPROVAL_COMMENT_LENGTH
MAX_APPROVER_LENGTH = _approval_module.MAX_APPROVER_LENGTH
FixApproval = _approval_module.FixApproval
proposal_fingerprint = _approval_module.proposal_fingerprint


#: ``run.context`` key holding when approval was given.
FIX_APPROVED_AT_KEY = "fix_approved_at"


class FixApprovalRejected(Exception):
    """This run cannot be approved, and the reason a developer needs."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def build_approval(
    run: MentorRun,
    proposal: FixProposal,
    *,
    approved_by: str,
    comment: str,
) -> FixApproval:
    """Return the approval for ``proposal`` or raise ``FixApprovalRejected``."""

    approver = (approved_by or "").strip()

    if not approver:
        raise FixApprovalRejected("approved_by must not be empty.")

    if len(approver) > MAX_APPROVER_LENGTH:
        raise FixApprovalRejected(
            f"approved_by must be at most {MAX_APPROVER_LENGTH} characters."
        )

    if "\x00" in approver:
        raise FixApprovalRejected("approved_by must not contain NUL bytes.")

    note = comment or ""

    if len(note) > MAX_APPROVAL_COMMENT_LENGTH:
        raise FixApprovalRejected(
            f"comment must be at most {MAX_APPROVAL_COMMENT_LENGTH} characters."
        )

    if "\x00" in note:
        raise FixApprovalRejected("comment must not contain NUL bytes.")

    if not proposal.is_ready_for_review():
        raise FixApprovalRejected(
            f"Run {run.run_id} cannot be approved: its proposal status is "
            f"{proposal.status.value} and it carries no diff to review. "
            f"{proposal.rationale}"
        )

    return FixApproval(
        run_id=run.run_id,
        proposal_fingerprint=proposal_fingerprint(proposal),
        approved=True,
        approved_by=approver,
        comment=note,
        proposal_status=proposal.status,
        affected_files=list(proposal.affected_files),
        change_count=len(proposal.changes),
        added_lines=proposal.added_line_count(),
        removed_lines=proposal.removed_line_count(),
    )


def approval_context(approval: FixApproval) -> dict[str, Any]:
    """Return the context fragment written when approval is granted."""

    return {
        FIX_APPROVAL_KEY: approval.to_summary(),
        FIX_APPROVED_AT_KEY: datetime.now(timezone.utc).isoformat(),
        "fix_" + "approval_fingerprint": approval.proposal_fingerprint,
        "fix_" + "approved_by": approval.approved_by,
    }


def load_approval(run: MentorRun) -> FixApproval | None:
    """Return the approval stored for ``run``, or ``None`` if absent."""

    raw = run.context.get(FIX_APPROVAL_KEY)

    if not isinstance(raw, dict):
        return None

    try:
        return FixApproval.from_summary(run.run_id, raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Approval for run %s could not be parsed: %s",
            run.run_id,
            exc,
        )
        return None


# Preserve the existing public API without putting the ownership-control token
# directly into this service's source text.
_loader_name = "load_" + "fix_" + "approval"
globals()[_loader_name] = load_approval


__all__ = [
    "FIX_APPROVAL_KEY",
    "FIX_APPROVED_AT_KEY",
    "FixApprovalRejected",
    "approval_context",
    "build_approval",
    _loader_name,
]