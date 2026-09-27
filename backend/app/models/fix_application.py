"""
Fix-application domain types — Phase 11 (applying an approved proposal).

The domain half of the write path. Pure Pydantic models, no I/O, no clock and
no LLM dependency, following the same convention as the other app.models
modules.

Why applying is modelled so defensively
---------------------------------------

Writing to somebody's source tree is the only irreversible thing this system
does, so the record of it has to be readable without trusting the writer. Three
decisions follow.

* **Every refusal is a named status, not an exception.** A proposal that was
  approved and then found to be stale, unsafe or unapplicable did not crash —
  it was *declined*, with a reason a developer can act on. ``REJECTED_STALE``
  and ``REJECTED_UNSAFE`` in particular are not interchangeable: the first
  means "the repository moved on, re-run the investigation", the second means
  "this change is not one this system will make, at all". Collapsing them into
  ``FAILED`` would lose the only information the developer needs.

* **Hashes, not diffs, are what is recorded.** ``AppliedFile`` stores the
  SHA-256 of the file before and after. A diff would tell you what was intended;
  a pair of digests tells you what is on disk now, and can be recomputed by
  anyone afterwards. The intended text is already in the stored proposal.

* **``rolled_back`` is reported, and is meaningful.** When a write fails part
  way through a multi-file change, the applier restores the files it already
  wrote and records that it did. A partial application left silently in place
  is the worst outcome available, and ``rolled_back=False`` on a failure is a
  claim the caller must be able to check, so the reason string says which files
  were restored.

What is *not* in this model
---------------------------

No source text, no diff body, no command, no absolute path. ``AppliedFile``
names repository-relative paths and digests only, so an application record can
be logged, streamed and returned over HTTP without a redaction pass.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


# ── Status ────────────────────────────────────────────────────────────────────


class FixApplicationStatus(str, Enum):
    """What happened when the approved proposal was applied.

    ``APPLIED``
        Every change was written, and the post-image was re-read and confirmed
        against what the proposal said it would write.

    ``ALREADY_APPLIED``
        The post-image was already exactly what the proposal would write. Not an
        error and not a second write: the requested end state holds, and saying
        so is more useful than pretending a change occurred.

    ``REJECTED_STALE``
        A file the proposal names no longer contains the exact text the proposal
        was built against, so the pre-image check failed. The remedy is to
        re-run the investigation; applying a line replacement to a file that
        has moved on is how a tool corrupts a repository.

    ``REJECTED_UNSAFE``
        A path the proposal names is one this system refuses to write: absolute,
        traversing, escaping the repository, or a secret file. No remedy short
        of not proposing it.

    ``REJECTED_MISMATCH``
        The approval does not match the proposal that is actually stored, so the
        thing that was approved is not the thing on record.

    ``FAILED_ROLLED_BACK``
        A write error occurred after at least one file had been modified. Every
        file the applier had written was restored to its pre-image, and the
        recorded digests are the restored ones.

    ``NOT_APPLIED``
        There was nothing to apply: no approval, or a proposal with no change.
        Reported rather than raised, because "the gate said no" is an answer the
        developer needs to see.
    """

    APPLIED = "APPLIED"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    REJECTED_STALE = "REJECTED_STALE"
    REJECTED_UNSAFE = "REJECTED_UNSAFE"
    REJECTED_MISMATCH = "REJECTED_MISMATCH"
    FAILED_ROLLED_BACK = "FAILED_ROLLED_BACK"
    NOT_APPLIED = "NOT_APPLIED"

    #: Not a pytest class — see ``TestPlanStatus`` for the same convention.
    __test__ = False

    @property
    def changed_repository(self) -> bool:
        """True only when this run left changed bytes behind in the tree.

        ``ALREADY_APPLIED`` is excluded because nothing was written, and
        ``FAILED_ROLLED_BACK`` is excluded because the write was deliberately
        undone — reporting that as "repository modified" would be the single
        most alarming thing this property could say and the least true. A
        rollback that could *not* restore a file is reported in the result's
        ``reason``, which says so by name.
        """

        return self is FixApplicationStatus.APPLIED


# ── Per-file record ───────────────────────────────────────────────────────────


class AppliedFile(BaseModel):
    """One repository-relative file that was the subject of the application.

    ``before_sha256`` and ``after_sha256`` are digests of the file's UTF-8 bytes
    on disk, not of the proposal's text. That distinction is the point: it is
    what makes the record independently checkable.
    """

    #: Repository-relative, POSIX-separated. Never absolute, never ``..``.
    file_path: str

    before_sha256: str

    after_sha256: str

    #: Number of source lines the proposal replaced in this file.
    lines_applied: int = 0

    #: True when ``before_sha256 != after_sha256`` — i.e. the bytes really moved.
    modified: bool = False

    @model_validator(mode="after")
    def _digests_must_be_hex(self) -> "AppliedFile":
        for name in ("before_sha256", "after_sha256"):
            value = getattr(self, name)

            if len(value) != 64 or any(
                char not in "0123456789abcdef" for char in value
            ):
                raise ValueError(
                    f"{name} must be 64 lowercase hexadecimal characters; got "
                    f"{value!r}"
                )

        if self.lines_applied < 0:
            raise ValueError("lines_applied must not be negative")

        return self


# ── The result ────────────────────────────────────────────────────────────────


class FixApplicationResult(BaseModel):
    """What applying one approved proposal to one repository actually did.

    ``verified`` is the load-bearing field and is False unless the applier
    re-read every file it wrote and found exactly the bytes the proposal
    predicted. It is False for every refusal, because a refusal wrote nothing
    and therefore verified nothing.
    """

    run_id: str

    status: FixApplicationStatus

    #: The proposal fingerprint that was applied. Echoed so a caller can
    #: confirm which proposal this record is about without a second lookup.
    proposal_fingerprint: str = ""

    #: Who approved it, as recorded at approval time. Empty when no approval
    #: existed, which is one of the ways ``NOT_APPLIED`` is detectable.
    approved_by: str = ""

    files: list[AppliedFile] = Field(default_factory=list)

    total_lines_applied: int = 0

    #: True when a write error occurred and every modified file was restored.
    rolled_back: bool = False

    #: True when the post-image was re-read and confirmed for every file.
    verified: bool = False

    #: Developer-facing explanation. Never contains source text.
    reason: str = ""

    #: Honest scope statements: what this application does and does not establish.
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _a_refusal_verifies_nothing(self) -> "FixApplicationResult":
        if self.verified and self.status not in (
            FixApplicationStatus.APPLIED,
            FixApplicationStatus.ALREADY_APPLIED,
        ):
            raise ValueError(
                f"status {self.status.value} writes no file, so it cannot have "
                f"verified a post-image"
            )

        if self.verified and not self.files:
            raise ValueError(
                "a verified application must record the files it verified; an "
                "empty file list is not evidence of anything"
            )

        if (
            self.rolled_back
            and self.status is not FixApplicationStatus.FAILED_ROLLED_BACK
        ):
            raise ValueError(
                "rolled_back is only meaningful alongside FAILED_ROLLED_BACK"
            )

        if self.total_lines_applied < 0:
            raise ValueError("total_lines_applied must not be negative")

        return self

    def modified_files(self) -> list[str]:
        """Repository-relative paths whose bytes actually changed."""

        return [f.file_path for f in self.files if f.modified]

    def applied(self) -> bool:
        """True only when the repository now holds the proposed change.

        Deliberately narrower than ``status is APPLIED``:

        ``ALREADY_APPLIED`` also leaves the repository holding the change, and a
        caller asking "is this fix in the working tree?" deserves a true.
        """

        return self.status in (
            FixApplicationStatus.APPLIED,
            FixApplicationStatus.ALREADY_APPLIED,
        )

    def as_event_payload(self) -> dict[str, Any]:
        """Payload for the ``FIX_APPLIED`` / ``FIX_APPLICATION_FAILED`` events.

        Paths, digests and counts only — never the diff, never the reason if it
        could carry a path outside the repository.
        """

        return {
            "status": self.status.value,
            "proposal_fingerprint": self.proposal_fingerprint,
            "file_count": len(self.files),
            "files": [
                {
                    "file_path": f.file_path,
                    "before_sha256": f.before_sha256,
                    "after_sha256": f.after_sha256,
                    "lines_applied": f.lines_applied,
                    "modified": f.modified,
                }
                for f in self.files
            ],
            "total_lines_applied": self.total_lines_applied,
            "rolled_back": self.rolled_back,
            "verified": self.verified,
            "repository_modified": self.status.changed_repository,
        }

    # ── Persistence helpers ─────────────────────────────────────────────

    def to_summary(self) -> dict[str, Any]:
        """Serialise the result for ``run.context``."""

        return self.model_dump(mode="json")

    @classmethod
    def from_summary(
        cls,
        run_id: str,
        summary: dict[str, Any],
    ) -> "FixApplicationResult":
        """Rebuild a result previously written by :meth:`to_summary`."""

        data = dict(summary)
        data["run_id"] = run_id
        return cls.model_validate(data)


__all__ = [
    "AppliedFile",
    "FixApplicationResult",
    "FixApplicationStatus",
]