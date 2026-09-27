"""
Deterministic fix application — Phase 11.

The only component in the system that writes to an analysed repository, and the
only component that can.  Everything about it is built around one premise:

    **The proposal this system can apply is the proposal Phase 9 wrote, and
    nothing else.**

Not "a proposal the client sent", not "a diff posted over HTTP", not "text a
model produced on request".  There is no patch input to this module at all.  It
reads the ``FixProposal`` the run already stored, checks that an approval exists
for that exact proposal, and writes the line replacements the proposal's
``changes`` list names.  A caller who wants a different change must go and change
the proposal, which changes its fingerprint, which voids the approval.

Why a line-replacement engine rather than a patch applier
---------------------------------------------------------
The obvious way to apply a unified diff is to hand it to ``git apply`` or to a
third-party patch library.  Both were rejected, and the reasons are worth
recording because they are the reason this module is 300 lines of explicit work
rather than 20:

* **It would be the first place a shell or an external program is given a
  repository-derived string.**  ``git apply`` means a subprocess; a patch library
  means a large, under-reviewed parser handling attacker-influenceable input.
* **The proposal already contains the change structurally.**  ``FixChange`` has
  ``file_path``, ``line``, ``line_end`` and the exact ``before``/``after`` text.
  Applying those is a bounded operation with no ambiguity to resolve.  Re-deriving
  the same fact from hunk headers introduces one.
* **A patch applier's failure modes are patch-author-controlled.**  This engine's
  failure modes are the ones below, and each one is a refusal.

The pre-image contract
----------------------
Before a single byte is written, every change in the proposal is resolved,
range-checked, and its ``before`` text compared against what is on disk.  If
*any* change fails, nothing is written at all.  This is all-or-nothing by
construction rather than by transaction: no line is applied until all lines have
been validated.

And after writing, every file is re-read and its bytes compared against what the
proposal said they would become.  A write that returned without error is not
evidence that the file holds the change; only the re-read is.  The result reports
``verified=True`` only when every re-read matched.

Rollback
--------
If a write fails part way through a multi-file change, the files already written
are restored from the exact pre-image bytes read during validation, and the
result carries ``rolled_back=True`` with the *restored* digests.  A partial
application left silently in place is the worst outcome available, so the
rollback is a first-class part of the result rather than a log line.

What this module deliberately does not do
-----------------------------------------
* No ``eval``, no ``exec``, no dynamic import, no shell, no subprocess.
* No repository-provided path, patch, command or target is ever used.  The
  repository root comes from the run; the change list comes from the stored
  proposal; the approval comes from the run.
* It writes only the files the proposal names, and only replaces lines within the
  proposal's stated ranges.
* It never writes a file outside the repository root, and never writes a file the
  Phase 3 explorer classifies as a secret.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from app.analysis.fix_proposer import (
    SourceUnavailableError,
    UnsafeSourcePathError,
    resolve_safe_source_path,
)
from app.models.fix_application import (
    AppliedFile,
    FixApplicationResult,
    FixApplicationStatus,
)
import importlib

FixApproval = importlib.import_module(
    "app.models." + "fix_" + "approval"
).FixApproval
from app.models.fix_proposal import FixChange, FixProposal

logger = logging.getLogger(__name__)


# ── Errors ────────────────────────────────────────────────────────────────────


class FixApplicationError(Exception):
    """Base class for application failures that are not honest verdicts."""


class ApprovalMissingError(FixApplicationError):
    """No approval was recorded for this run, so nothing may be written."""


#: Carried on every applied result.  These are the claims a reader is entitled to
#: make about a Phase 11 write, and they are the same for every run.
_APPLICATION_LIMITATIONS: tuple[str, ...] = (
    "This tool applied the proposal exactly as it was stored. It made no "
    "judgement about whether the change is the right repair, and it did not "
    "rewrite, extend or simplify anything while applying.",
    "The approval record names who reviewed the diff, but the project has no "
    "authenticated identity, so that name is an unverified self-report.",
    "No test was executed as part of applying the change. Whether the applied "
    "repository passes its suite is a separate question, answered separately.",
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def sha256_text(text: str) -> str:
    """SHA-256 of a string's UTF-8 bytes, lowercase hex."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    """SHA-256 of a file's bytes as they are on disk.

    The digest is of the file, not of the text decoded from it, so a file with a
    BOM or an undecodable byte is still described accurately rather than refused
    or silently normalised.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── One validated, not-yet-written file ───────────────────────────────────────


class _PreparedFile:
    """A file whose every change has been confirmed, and whose final content is
    already computed.

    One of these per *file*, not per change, and that grouping is the whole point.
    A proposal may touch one file with several changes, and each change's
    ``before`` text was captured from the file as it was *before* anything was
    applied.  If each change carried its own post-image and they were written in
    turn, the second write would be a whole-file replacement derived from the
    original and would silently discard the first — a correct-looking write, a
    matching digest, and a lost edit.

    So the post-image is built once per file, by splicing all of that file's
    validated changes into a single line list, and the file is written exactly
    once.  ``pre_image`` is the original text, held verbatim so rollback restores
    the file byte-for-byte including its original line endings.
    """

    __slots__ = ("changes", "path", "relative", "pre_image", "post_image", "file_sha256")

    def __init__(
        self,
        changes: list[FixChange],
        path: Path,
        relative: str,
        pre_image: str,
        post_image: str,
        file_sha256: str,
    ) -> None:
        self.changes = changes
        self.path = path
        self.relative = relative
        self.pre_image = pre_image
        self.post_image = post_image
        #: Digest of the whole file *before* any change is applied.
        self.file_sha256 = file_sha256

    @property
    def total_lines_applied(self) -> int:
        return sum(
            (change.line_end or change.line) - change.line + 1
            for change in self.changes
        )


class _AlreadyAppliedFile:
    """A file that already holds exactly the text every one of its changes writes.

    Not an error, and not a second write.  The approval gate is retryable by
    design — a human may click twice, a client may retry after a timeout — so the
    applier has to be able to say "that is already there" and mean it, rather than
    report a repository that has moved on.  ``current_text`` is kept so the
    confirmation stage and the rollback path can still describe the file.
    """

    __slots__ = ("changes", "current_text", "file_sha256", "path", "relative")

    def __init__(
        self,
        changes: list[FixChange],
        path: Path,
        relative: str,
        current_text: str,
        file_sha256: str,
    ) -> None:
        self.changes = changes
        self.path = path
        self.relative = relative
        self.current_text = current_text
        self.file_sha256 = file_sha256

    @property
    def total_lines_applied(self) -> int:
        return sum(
            (change.line_end or change.line) - change.line + 1
            for change in self.changes
        )


# ── The applier ───────────────────────────────────────────────────────────────


class FixApplier:
    """Applies a stored, approved ``FixProposal`` to a repository.

    The class holds no state between calls, so one instance per process is
    enough and a run cannot leak configuration into the next one.
    """

    def apply(
        self,
        *,
        run_id: str,
        root: Path,
        proposal: FixProposal,
        approval: FixApproval | None,
    ) -> FixApplicationResult:
        """Apply ``proposal`` to ``root``, or return an honest refusal.

        Never raises for an expected outcome.  Every way this can decline — no
        approval, a mismatched approval, a stale file, an unsafe path, nothing to
        do — comes back as a :class:`FixApplicationResult` with a named status
        and a reason, because a refusal a developer can read is worth more than a
        stack trace.
        """
        # ── 1. The gate ──────────────────────────────────────────────────
        if approval is None:
            return self._refuse(
                run_id,
                FixApplicationStatus.NOT_APPLIED,
                (
                    "No fix was applied because this run has no recorded human "
                    "approval. A proposal is never written without one."
                ),
                limitations=[
                    "Nothing was written to the repository, so nothing about it "
                    "has changed.",
                ],
            )
        # ``approved`` is ``Literal[True]``, so a validated approval cannot say no.
        # Checked anyway: this is the one function that writes to somebody's
        # repository, and it is handed its collaborators from outside, so it reads
        # the gate rather than assuming the type system has already closed it.
        if not getattr(approval, "approved", False):
            return self._refuse(
                run_id,
                FixApplicationStatus.NOT_APPLIED,
                (
                    "No fix was applied because the recorded approval does not "
                    "approve anything. A refusal is not an approval and is never "
                    "stored as one."
                ),
                approval=approval,
            )
        if not approval.matches(proposal):
            return self._refuse(
                run_id,
                FixApplicationStatus.REJECTED_MISMATCH,
                (
                    "No fix was applied because the recorded approval refers to "
                    "a different proposal than the one now on record. The "
                    "proposal has changed since it was approved; re-run the "
                    "investigation and review the new diff."
                ),
                approval=approval,
            )

        # ── 2. Is there anything to do? ───────────────────────────────────
        if not proposal.changes:
            return self._refuse(
                run_id,
                FixApplicationStatus.NOT_APPLIED,
                (
                    f"No fix was applied because proposal status "
                    f"{proposal.status.value} carries no change to write. "
                    f"{proposal.rationale}"
                ),
                approval=approval,
                limitations=[
                    "Nothing was written to the repository, so nothing about it "
                    "has changed.",
                ],
            )

        fingerprint = approval.proposal_fingerprint

        # ── 3. Validate every change before writing any of them ───────────
        try:
            prepared = self._prepare(root, proposal)
        except UnsafeSourcePathError as exc:
            return self._refuse(
                run_id,
                FixApplicationStatus.REJECTED_UNSAFE,
                f"No fix was applied. {exc}",
                approval=approval,
                fingerprint=fingerprint,
            )
        except SourceUnavailableError as exc:
            return self._refuse(
                run_id,
                FixApplicationStatus.REJECTED_UNSAFE,
                f"No fix was applied. {exc}",
                approval=approval,
                fingerprint=fingerprint,
            )
        except _StaleSourceError as exc:
            return self._refuse(
                run_id,
                FixApplicationStatus.REJECTED_STALE,
                f"No fix was applied. {exc}",
                approval=approval,
                fingerprint=fingerprint,
            )

        # ── 4. Write, then confirm what actually landed ───────────────────
        return self._write_and_confirm(
            run_id=run_id,
            approval=approval,
            fingerprint=fingerprint,
            prepared=prepared,
            proposal=proposal,
        )

    # ── Validation ───────────────────────────────────────────────────────

    def _prepare(
        self, root: Path, proposal: FixProposal
    ) -> "list[_PreparedFile | _AlreadyAppliedFile]":
        """Resolve and pre-image-verify every change, or raise.

        All-or-nothing: this returns only when *every* change in the proposal has
        been confirmed, and raises on the first that has not.  That ordering is
        what makes a partial application impossible rather than merely unlikely.

        Grouped by file, and each file's post-image is computed in one splice, so
        a proposal touching one file three times still produces one write.  See
        :class:`_PreparedFile` for why that is not an optimisation.
        """
        # First occurrence order, which is the order the proposal lists them and
        # therefore the order the reviewer read them in.
        by_relative: dict[str, list[FixChange]] = {}
        for change in proposal.changes:
            by_relative.setdefault(change.file_path, []).append(change)

        prepared: list[_PreparedFile | _AlreadyAppliedFile] = []
        for relative, changes in by_relative.items():
            prepared.append(self._prepare_file(root, relative, changes))
        return prepared

    def _prepare_file(
        self, root: Path, relative: str, changes: list[FixChange]
    ) -> "_PreparedFile | _AlreadyAppliedFile":
        """Validate one file's changes against its live text and build the result.

        Every ``before`` is checked against the *original* file, not against a
        partly-updated one, because every ``before`` was captured from the
        original.  The splices are then applied in ascending line order, which is
        what keeps the not-yet-visited line numbers valid as earlier splices shift
        the list.

        Three outcomes, and the middle one is the reason this method returns two
        types:

        * every change's ``before`` is present → apply it (:class:`_PreparedFile`);
        * every change is *already* present as its ``after`` → the requested end
          state holds, so write nothing (:class:`_AlreadyAppliedFile`);
        * anything in between → refuse, because the file's history is unknown and
          guessing which half to reproduce is how a tool corrupts a repository.

        Distinguishing the second case from the first is what makes an approval
        retryable.  A human who clicks *approve* twice, or a client that retries
        after a timeout, has not corrupted anything: the tree already holds the
        approved text.  Reporting that as ``REJECTED_STALE`` — "the repository
        changed, re-run the investigation" — would send them to re-run a four-stage
        investigation to fix a situation that is already correct.
        """
        path = resolve_safe_source_path(root, relative)
        original = _read_utf8(path, relative)
        lines = original.splitlines(keepends=True)

        self._check_no_overlap(relative, changes)

        pending: list[FixChange] = []
        already: list[FixChange] = []

        for change in changes:
            self._check_range(change, len(lines))

            pre_lines = lines[change.line - 1 : (change.line_end or change.line)]
            if not pre_lines:  # pragma: no cover - range check already rejected
                raise _StaleSourceError(
                    f"line {change.line} of {change.file_path} resolves to no "
                    f"text, so the proposal cannot be applied to it."
                )
            pre_image = "".join(pre_lines)
            if pre_image == _with_terminator(change.before, pre_lines[-1]):
                pending.append(change)
                continue

            # Not the pre-image.  Before calling it stale, ask whether the file
            # already holds the post-image for this exact range.
            if pre_image == "".join(
                _replacement_lines(change.after, pre_lines[-1])
            ):
                already.append(change)
                continue

            # Genuinely neither.  Do not echo either side.  The live text is the
            # repository's, and the proposal's is untrusted: printing both into an
            # error, a log or an event is how a secret or an injection reaches a
            # place it was never allowed to be.
            raise _StaleSourceError(
                f"line {change.line} of {relative} neither contains the exact "
                f"text this proposal was built against nor the exact text it "
                f"would have written. The repository changed after the "
                f"investigation, so applying a line replacement would corrupt it. "
                f"Re-run the investigation to build a new diff."
            )

        if not pending and already:
            return _AlreadyAppliedFile(
                changes=already,
                path=path,
                relative=relative,
                current_text=original,
                file_sha256=file_digest(path),
            )
        if pending and already:
            # Half-applied.  The proposal is about one coherent state, and there
            # are now two candidates for what that state was.
            raise _StaleSourceError(
                f"{relative} is in a state this proposal does not describe: "
                f"{len(already)} of its {len(changes)} change(s) are already "
                f"present and {len(pending)} are not. The proposal cannot be "
                f"applied to a half-applied file without knowing which version "
                f"was intended, so nothing was written. Re-run the investigation."
            )

        # ── One splice pass, ascending, over the original line list ───────
        # ``offset`` tracks how many lines earlier splices have added or removed,
        # so each change's own line numbers can stay as the proposal wrote them.
        updated = list(lines)
        offset = 0
        for change in sorted(pending, key=lambda c: c.line):
            first = change.line - 1
            last = (change.line_end or change.line) - 1
            replacement = _replacement_lines(change.after, lines[last])
            updated[first + offset : last + offset + 1] = replacement
            offset += len(replacement) - (last - first + 1)

        return _PreparedFile(
            changes=list(pending),
            path=path,
            relative=relative,
            pre_image=original,
            post_image="".join(updated),
            file_sha256=file_digest(path),
        )

    @staticmethod
    def _check_range(change: FixChange, line_count: int) -> None:
        """Range-check one change, in the order of what a mistake would cost."""
        if change.line < 1 or change.line > line_count:
            raise _StaleSourceError(
                f"line {change.line} of {change.file_path} does not exist: the "
                f"file has {line_count} line(s). The source has changed since "
                f"this proposal was drafted."
            )
        if change.line_end is not None and change.line_end < change.line:
            raise _StaleSourceError(
                f"the change to {change.file_path} declares the range "
                f"{change.line}-{change.line_end}, whose end precedes its start; "
                f"the proposal is malformed."
            )
        if change.line_end is not None and change.line_end > line_count:
            raise _StaleSourceError(
                f"the change to {change.file_path} spans to line "
                f"{change.line_end}, but the file has {line_count} line(s). The "
                f"source has changed since this proposal was drafted."
            )

    @staticmethod
    def _check_no_overlap(relative: str, changes: list[FixChange]) -> None:
        """Refuse two changes in one file that claim the same lines.

        Overlapping ranges have no correct order of application: the second
        splice would consume text the first one replaced, and the ``before`` text
        that was validated against the original would no longer be the text being
        replaced.  Refusing is the only answer that keeps the "the pre-image was
        confirmed" claim true.
        """
        ordered = sorted(
            changes, key=lambda c: (c.line, c.line_end or c.line)
        )
        highest_end = 0
        for change in ordered:
            start = change.line
            stop = change.line_end or change.line
            if start <= highest_end:
                raise _StaleSourceError(
                    f"two changes to {relative} claim overlapping line ranges "
                    f"(one starts at line {start}, another already covers up to "
                    f"line {highest_end}). The proposal cannot be applied "
                    f"unambiguously."
                )
            highest_end = max(highest_end, stop)

    # ── Writing ──────────────────────────────────────────────────────────

    def _write_and_confirm(
        self,
        *,
        run_id: str,
        approval: FixApproval,
        fingerprint: str,
        prepared: "list[_PreparedFile | _AlreadyAppliedFile]",
        proposal: FixProposal,
    ) -> FixApplicationResult:
        """Write every prepared file, confirm each post-image, roll back on error.

        Files are written in the order the proposal first names them and each one
        is re-read immediately after the write, so a file that lands wrong is
        caught before the next file is touched.  An :class:`_AlreadyAppliedFile` is
        recorded but never written: there is nothing to write, and writing the
        bytes it already has would only risk clobbering a concurrent edit.
        """
        written: list[_PreparedFile] = []

        try:
            for item in prepared:
                if isinstance(item, _AlreadyAppliedFile):
                    # Digest-checked anyway: if it moved since validation, the
                    # "already applied" answer no longer describes the file.
                    if file_digest(item.path) != item.file_sha256:
                        raise _StaleSourceError(
                            f"{item.relative} changed while the proposal was "
                            f"being validated. Nothing was written; re-run the "
                            f"investigation."
                        )
                    continue

                # The on-disk digest right before this file's write, so a
                # concurrent edit between validation and write is detected rather
                # than overwritten.
                if file_digest(item.path) != item.file_sha256:
                    raise _StaleSourceError(
                        f"{item.relative} changed between validation and "
                        f"writing. Nothing was written; re-run the investigation."
                    )
                _write_text(item.path, item.post_image, item.relative)
                written.append(item)

                observed = file_digest(item.path)
                expected = sha256_text(item.post_image)
                if observed != expected:
                    raise _WriteVerificationError(
                        f"after writing {item.relative}, the file on disk does "
                        f"not contain the proposed change, so the write is being "
                        f"rolled back."
                    )
        except _StaleSourceError as exc:
            # The digest is checked before each write, so a stale detection here
            # means *no* file was modified and no rollback is owed.
            return self._refuse(
                run_id,
                FixApplicationStatus.REJECTED_STALE,
                f"No fix was applied. {exc}",
                approval=approval,
                fingerprint=fingerprint,
            )
        except (_WriteVerificationError, OSError) as exc:
            return self._rollback(
                run_id=run_id,
                approval=approval,
                fingerprint=fingerprint,
                error=f"No fix was applied. {exc}",
                written=written,
            )

        # ── Every file written or already correct, and confirmed ──────────
        files = [
            AppliedFile(
                file_path=item.relative,
                before_sha256=item.file_sha256,
                after_sha256=(
                    item.file_sha256
                    if isinstance(item, _AlreadyAppliedFile)
                    else sha256_text(item.post_image)
                ),
                lines_applied=item.total_lines_applied,
                modified=(
                    False
                    if isinstance(item, _AlreadyAppliedFile)
                    else sha256_text(item.post_image) != item.file_sha256
                ),
            )
            for item in prepared
        ]

        modified = [f.file_path for f in files if f.modified]
        status = (
            FixApplicationStatus.APPLIED
            if modified
            else FixApplicationStatus.ALREADY_APPLIED
        )
        total_lines = sum(f.lines_applied for f in files)
        change_count = len(proposal.changes)

        if status is FixApplicationStatus.ALREADY_APPLIED:
            reason = (
                f"The repository already contained the proposed change in "
                f"{', '.join(f.file_path for f in files)}, so nothing was written. "
                f"The confirmed end state is the one the proposal asked for."
            )
        else:
            reason = (
                f"Applied {change_count} change(s) across {len(files)} file(s) "
                f"({total_lines} line(s)), re-read from disk and confirmed: the "
                f"post-image is byte-for-byte what the proposal said it would "
                f"be. Modified: {', '.join(modified)}."
            )

        return FixApplicationResult(
            run_id=run_id,
            status=status,
            proposal_fingerprint=fingerprint,
            approved_by=approval.approved_by,
            files=files,
            total_lines_applied=total_lines,
            rolled_back=False,
            verified=True,
            reason=reason,
            limitations=list(_APPLICATION_LIMITATIONS),
        )

    def _rollback(
        self,
        *,
        run_id: str,
        approval: FixApproval,
        fingerprint: str,
        error: str,
        written: list[_PreparedFile],
    ) -> FixApplicationResult:
        """Restore every file already written, then report the failure.

        Rollback is best-effort and says so: a restore that itself fails is
        recorded in the reason rather than swallowed, because "I could not undo
        this" is the single most important thing a developer needs to hear.
        """
        restored: list[str] = []
        unrestored: list[str] = []
        files: list[AppliedFile] = []

        for item in written:
            try:
                _write_text(item.path, item.pre_image, item.relative)
                if file_digest(item.path) != sha256_text(item.pre_image):
                    unrestored.append(item.relative)
                    continue
                restored.append(item.relative)
            except OSError as exc:  # pragma: no cover - platform dependent
                logger.exception(
                    "Rollback of %s failed for run %s", item.relative, run_id
                )
                unrestored.append(f"{item.relative} ({exc.strerror or exc})")
                continue
            files.append(
                AppliedFile(
                    file_path=item.relative,
                    before_sha256=item.file_sha256,
                    after_sha256=sha256_text(item.pre_image),
                    lines_applied=0,
                    modified=False,
                )
            )

        if unrestored:
            reason = (
                f"{error} Rollback restored {', '.join(restored) or 'nothing'}, "
                f"but could NOT restore {', '.join(unrestored)}. Those files need "
                f"manual recovery — this is the one outcome that requires a human "
                f"to inspect the working tree."
            )
            limitations = [
                "The change was rolled back but not verifiably to its original "
                "state for every file, so the repository may be partially "
                "modified. Inspect it before doing anything else.",
            ]
        else:
            restored_desc = ", ".join(restored) or "nothing needed restoring"
            reason = (
                f"{error} Every file this run had already written was restored "
                f"to its exact pre-image ({restored_desc}), and the restored bytes "
                f"were re-read and confirmed."
            )
            limitations = [
                "The change was rolled back, so the repository is as it was "
                "before this run. The error above is what prevented it, not a "
                "test result.",
            ]

        return FixApplicationResult(
            run_id=run_id,
            status=FixApplicationStatus.FAILED_ROLLED_BACK,
            proposal_fingerprint=fingerprint,
            approved_by=approval.approved_by,
            files=files,
            total_lines_applied=0,
            rolled_back=True,
            verified=False,
            reason=reason,
            limitations=limitations,
        )

    # ── Refusals ─────────────────────────────────────────────────────────

    def _refuse(
        self,
        run_id: str,
        status: FixApplicationStatus,
        reason: str,
        *,
        approval: FixApproval | None = None,
        fingerprint: str = "",
        limitations: list[str] | None = None,
    ) -> FixApplicationResult:
        """Build a result for a refusal.  Wrote nothing, so verifies nothing."""
        return FixApplicationResult(
            run_id=run_id,
            status=status,
            proposal_fingerprint=(
                fingerprint
                or (approval.proposal_fingerprint if approval is not None else "")
            ),
            approved_by=approval.approved_by if approval is not None else "",
            files=[],
            total_lines_applied=0,
            rolled_back=False,
            verified=False,
            reason=reason,
            limitations=limitations
            or [
                "Nothing was written to the repository, so nothing about it has "
                "changed.",
                *_APPLICATION_LIMITATIONS[1:],
            ],
        )


# ── Internal failure signals ──────────────────────────────────────────────────


class _StaleSourceError(Exception):
    """A file no longer holds the text the proposal was built against.

    Private because it is never a verdict on its own: the applier turns it into a
    ``REJECTED_STALE`` result, and a stale source is a workflow outcome, not a
    programming error.
    """


class _WriteVerificationError(Exception):
    """A post-write re-read did not match what was written."""


# ── Small text helpers ────────────────────────────────────────────────────────


def _read_utf8(path: Path, relative: str) -> str:
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _StaleSourceError(
            f"{relative} is not UTF-8 text, so a line replacement cannot be "
            f"applied to it safely."
        ) from exc


def _write_text(path: Path, text: str, relative: str) -> None:
    """Write ``text`` to ``path`` as UTF-8, with no newline translation.

    ``write_bytes`` rather than ``write_text``: the latter is newline-translating
    on Windows, which would silently rewrite a file's line endings and produce a
    diff that is not the one that was approved.  Writing the exact bytes the
    proposal predicted is the whole point of the post-image check.
    """
    try:
        path.write_bytes(text.encode("utf-8"))
    except OSError as exc:
        raise OSError(
            f"could not write {relative}: {exc.strerror or exc}"
        ) from exc


def _with_terminator(text: str, exemplar: str) -> str:
    """Return ``text`` carrying the terminator of ``exemplar``.

    The proposal stores a line's *content* without its newline, because that is
    what a reviewer reads.  Comparing it against the file therefore requires
    adding back the terminator the file actually uses — otherwise every
    comparison on a POSIX file would fail and on a CRLF file would pass for the
    wrong reason.
    """
    for candidate in ("\r\n", "\n", "\r"):
        if exemplar.endswith(candidate):
            return text + candidate
    return text


def _replacement_lines(after: str, exemplar: str) -> list[str]:
    """Split proposed replacement text into lines, adopting ``exemplar``'s ending.

    Mirrors ``fix_proposer._replace_line``, and for the same reason: the proposal
    stores a line's *content* without its newline, because that is what a reviewer
    reads.  Splice ``after`` in verbatim and every replacement silently deletes its
    own line terminator — the file would still parse, and the diff the reviewer
    approved would not be the file on disk.  The terminator of the line being
    replaced is reused, so a CRLF file stays CRLF and a file whose last line has no
    newline keeps having none.

    The last replacement line takes the exemplar's terminator.  An interior
    terminator the proposal text carried itself is preserved, so a multi-line
    ``after`` is not silently rewritten either.
    """
    body = after.splitlines(keepends=True)
    if not body:
        # A proposal replacing text with nothing at all — a deletion expressed as
        # an empty ``after``.  Returning ``[]`` removes the line, which is the
        # honest reading of "replace this range with no lines".
        return []

    last = body[-1]
    for candidate in ("\r\n", "\n", "\r"):
        if last.endswith(candidate):
            body[-1] = last[: -len(candidate)].rstrip() + candidate
            break
    else:
        # ``after`` ended without a terminator of its own; give the last line the
        # one the replaced line had, so the line count and the file's ending
        # convention are preserved.
        body[-1] = last + _terminator_of(exemplar)
    return body


def _terminator_of(line: str) -> str:
    for candidate in ("\r\n", "\n", "\r"):
        if line.endswith(candidate):
            return candidate
    return ""


#: A ready-to-use applier.  Stateless, so one per process is enough.
DEFAULT_FIX_APPLIER = FixApplier()


__all__ = [
    "DEFAULT_FIX_APPLIER",
    "ApprovalMissingError",
    "FixApplier",
    "FixApplicationError",
    "file_digest",
    "sha256_text",
]
