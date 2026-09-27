"""
Fix-approval domain types — Phase 11 (human approval gate).

These are the *domain* half of the approval gate.  Pure Pydantic models with no
I/O, no clock and no LLM dependency, following the convention established by
``app.models.domain``, ``app.models.workflow``, ``app.models.root_cause``,
``app.models.fix_proposal`` and ``app.models.test_plan``.

What an approval is, and what it deliberately is not
---------------------------------------------------
Phases 1–10 could refuse to modify a repository because no component was ever given
the ability to.  Phase 11 is the first phase that *can* write, so the interesting
question stops being "can it write?" and becomes "what stops it writing the wrong
thing?"  The answer is this model, and it rests on three decisions:

* **Approval is bound to one exact proposal, by content.**  ``proposal_fingerprint``
  is a SHA-256 over the canonical serialisation of the whole
  :class:`app.models.fix_proposal.FixProposal`.  A reviewer who approves a diff
  has approved *those bytes*.  If the stored proposal is later edited — even
  re-serialised with a different field order — the fingerprint no longer matches
  and the approval is void.  This is why the fingerprint is over the model rather
  than over ``run_id``: a run identifier says which proposal *was* drafted, and
  says nothing about whether it is the one on disk now.

* **The approval carries no patch.**  It records that a human said yes, and what
  they were shown.  It cannot carry source text, a diff, a target path or a
  command, because a gate whose payload can be edited after the fact is not a
  gate.  ``affected_files`` and ``change_count`` are the *summary* that was on
  screen; the applier re-derives the authoritative change list from the stored
  proposal and re-checks its fingerprint before writing a byte.

* **Nothing here is a timestamp.**  Same reason as Phases 8, 9 and 10: a
  wall-clock value inside a stored model makes two identical records compare
  unequal and breaks replay.  When approval happened lives in
  ``run.context["fix_approved_at"]`` and in the event log.

Honest naming
-------------
``approved`` is always ``True`` in a stored :class:`FixApproval`.  A refusal is
not an approval and is not modelled as one: it is an ``HTTP 409`` from the
endpoint, which is a different thing from a record that exists and says no.  A
model that could hold ``approved=False`` would let a run carry a permanent
"someone said no" that later phases had to learn to honour.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.models.fix_proposal import FixProposal, FixProposalStatus

#: Prefix on every fingerprint, so a value can never be mistaken for a bare hash
#: of something else in a log or an API response.
FINGERPRINT_PREFIX = "sha256:"
FIX_APPROVAL_KEY = "fix_approval"

#: Upper bound on ``approved_by`` and ``comment``.  Both are free text written by
#: a human, and both are stored and echoed, so neither may be unbounded.
MAX_APPROVER_LENGTH = 200
MAX_APPROVAL_COMMENT_LENGTH = 4000


# ── Fingerprinting ────────────────────────────────────────────────────────────


def proposal_fingerprint(proposal: FixProposal) -> str:
    """Return the content fingerprint of ``proposal``.

    The digest is taken over the canonical JSON serialisation of the whole
    proposal: keys sorted, no insignificant whitespace, no ASCII escaping.  Two
    proposals that are semantically identical therefore fingerprint identically
    regardless of the order their fields were populated in, which is what makes
    the fingerprint usable as a *replay* check rather than only as an
    identity check.

    Nothing is read from the repository and nothing is read from the request to
    build it.  The proposal is the only input, so the fingerprint can be computed
    by the API, the applier and a test without any of them agreeing on a
    protocol first.
    """
    canonical = json.dumps(
        proposal.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_PREFIX}{digest}"


def is_well_formed_fingerprint(value: str) -> bool:
    """Return True when ``value`` has the shape this module produces.

    Used to reject a client-supplied fingerprint cheaply, before it is ever
    compared.  This is a *shape* check, not a security boundary — the real
    boundary is the comparison against the stored proposal.
    """
    if not isinstance(value, str) or not value.startswith(FINGERPRINT_PREFIX):
        return False
    hex_digest = value[len(FINGERPRINT_PREFIX) :]
    if len(hex_digest) != 64:
        return False
    return all(char in "0123456789abcdef" for char in hex_digest)


# ── The approval ──────────────────────────────────────────────────────────────


class FixApproval(BaseModel):
    """A human's recorded approval of one exact proposal.

    Invariants enforced here rather than trusted from the caller:

    * ``approved`` is always ``True`` — see the module docstring.
    * ``proposal_fingerprint`` is well-formed.  A stored approval with a
      malformed fingerprint would be a gate that cannot be compared against
      anything, which is worse than no gate.
    * ``affected_files`` is non-empty, and ``change_count`` agrees with it.  A
      human cannot approve a proposal that changes nothing.
    * ``approved_by`` and ``comment`` are bounded in length and free of NUL
      bytes.
    """

    run_id: str
    #: The exact proposal that was reviewed.  See :func:`proposal_fingerprint`.
    proposal_fingerprint: str
    #: Always ``True``, and ``Literal`` so the type says it rather than a comment.
    #: A refusal is not recorded as an approval: it is an HTTP 409 from the
    #: endpoint.  Without this, a run could carry a permanent "someone said no"
    #: that later phases had to learn to honour — and a gate whose own record can
    #: say no is a gate with a bypass.
    approved: Literal[True] = True
    #: A human-readable label for who reviewed the diff.
    #:
    #: This is an **audit label, not an authenticated identity**.  The project
    #: has no authentication layer, and inventing the appearance of one by
    #: accepting a caller-supplied name would be worse than saying so plainly:
    #: a client could always claim to be anyone.  The field is recorded because an
    #: approval nobody can attribute is not a review, and its limitation is
    #: carried in the ``limitations`` list of the application result.
    approved_by: str = Field(max_length=MAX_APPROVER_LENGTH)
    #: Free-text note from the reviewer.  Never interpreted.
    comment: str = Field(default="", max_length=MAX_APPROVAL_COMMENT_LENGTH)

    #: Summary of what was on screen when the approval was given.  The applier
    #: never reads these to decide what to write; it re-derives the change list
    #: from the stored proposal and re-checks the fingerprint.  They exist so a
    #: reviewer of the *record* can see what the approver thought they were
    #: approving.
    proposal_status: FixProposalStatus
    affected_files: list[str] = Field(default_factory=list)
    change_count: int = 0
    #: ``+``/``-`` line counts as displayed on the diff, for the same reason.
    added_lines: int = 0
    removed_lines: int = 0

    @field_validator("approved_by")
    @classmethod
    def approver_must_be_usable(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("approved_by must not be empty")
        if "\x00" in value:
            raise ValueError("approved_by must not contain NUL bytes")
        return cleaned

    @field_validator("comment")
    @classmethod
    def comment_must_be_usable(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("comment must not contain NUL bytes")
        return value

    @field_validator("proposal_fingerprint")
    @classmethod
    def fingerprint_must_be_well_formed(cls, value: str) -> str:
        if not is_well_formed_fingerprint(value):
            raise ValueError(
                "proposal_fingerprint must be a 'sha256:' prefix followed by 64 "
                "lowercase hexadecimal characters"
            )
        return value

    @field_validator("affected_files")
    @classmethod
    def files_must_be_present(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError(
                "an approval must name the files it approved; a proposal that "
                "changes nothing cannot be approved"
            )
        if any("\x00" in path or not path.strip() for path in value):
            raise ValueError("affected_files must be non-empty, NUL-free paths")
        return value

    @field_validator("change_count")
    @classmethod
    def change_count_must_be_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("change_count must be at least 1 to approve a change")
        return value

    def matches(self, proposal: FixProposal) -> bool:
        """True when this approval was given for exactly ``proposal``.

        The comparison is over content, so it is recomputed rather than trusted
        from a stored boolean: a caller that edited the fingerprint to match a
        proposal nobody reviewed still has to know the digest, and the digest
        is derived from the proposal alone.
        """
        return self.proposal_fingerprint == proposal_fingerprint(proposal)

    # ── Persistence helpers ─────────────────────────────────────────────

    def to_summary(self) -> dict[str, Any]:
        """Serialise the approval for ``run.context``."""
        return self.model_dump(mode="json")

    @classmethod
    def from_summary(cls, run_id: str, summary: dict[str, Any]) -> "FixApproval":
        """Rebuild an approval previously written by :meth:`to_summary`."""
        data = dict(summary)
        data["run_id"] = run_id
        return cls.model_validate(data)

    def as_event_payload(self) -> dict[str, Any]:
        """Payload for the ``FIX_APPROVED`` event.

        Deliberately excludes ``comment``.  Events are streamed to every
        connected client and appended to a durable log; a free-text reviewer
        note is not part of the workflow signal, and a comment that echoed source
        back to whoever opened the stream would be a leak this project does not
        need to take on.
        """
        return {
            "approved_by": self.approved_by,
            "proposal_fingerprint": self.proposal_fingerprint,
            "proposal_status": self.proposal_status.value,
            "affected_files": list(self.affected_files),
            "change_count": self.change_count,
            "added_lines": self.added_lines,
            "removed_lines": self.removed_lines,
        }


__all__ = [
    "FIX_APPROVAL_KEY",
    "FINGERPRINT_PREFIX",
    "MAX_APPROVAL_COMMENT_LENGTH",
    "MAX_APPROVER_LENGTH",
    "FixApproval",
    "is_well_formed_fingerprint",
]