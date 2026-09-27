"""
Verification — Phase 11.

One stage, at the state Phase 11 owns:

``VERIFICATION``
    :class:`VerificationStage` — run the repository's *existing* tests against the
    tree the approved fix was written to, and report what the runner's exit code
    actually established.

This is the only stage in the system that can produce a ``VERIFIED_RESULT``, and
the only one that may.  Everything about it is arranged so that the strength of the
claim is decided by the exit code and by nothing else.

Which tests get run
-------------------
The Phase 10 test plan's scenarios, and only those naming a file that
:class:`app.models.test_plan.TestFileStatus` marks ``EXISTING``.

That is a deliberately small surface:

* A scenario marked ``PROPOSED_NEW`` names a path inferred from an observed
  naming convention.  The file is not there.  Running it would either fail
  collection or, worse, run whatever happens to be at that path.
* The list is derived from evidence Phase 7 already recorded on the run, copied
  verbatim by the Phase 10 planner.  It is therefore a *server* decision about
  *which files to point a runner at* — it is not a command, and no string from it
  reaches an argument vector except as a validated repository-relative path that
  the executor checks resolves to a real file inside the run's own root.
* If the plan names no existing file, verification is ``BLOCKED``.  It does not
  fall back to "run the whole suite", because a whole-suite run is a different
  experiment with a different claim attached, and quietly widening the scope of
  what a system verified is exactly the failure mode this project is about.

Two questions get separate answers
----------------------------------
``regression_check``
    The repository's own suite passed against the applied change.  This is what
    Phase 11 can establish, and it is worth having: a one-line arithmetic rewrite
    that silently breaks an unrelated expectation is the most likely way an
    "obviously correct" fix causes damage.

``defect_demonstrated``
    A test that discriminates the diagnosed behaviour from correct behaviour
    passed.  Phase 11 writes no test source, so this is true only when a
    pre-existing test already discriminates.  It is decided by a **counterfactual
    run**: the identical test targets are re-run against a scratch copy of the
    repository with the applied change reverted, and the flag is set only if that
    run really failed.  A green suite on its own proves nothing — ShopDemo's own
    seven tests pass on the broken code too — and neither does the test plan
    *asking* for a discriminating test.  Every way the counterfactual fails to
    decide yields ``False``, so on the demo repository it comes out **false** and
    the result says so: the suite passing there shows the change broke nothing,
    and nothing whatsoever about the discount arithmetic.

Collapsing those two into one boolean is the single most likely way this phase
could produce a demo that lies, so they are separate fields and
``defect_demonstrated=False`` is a perfectly ordinary, non-failing outcome.

Before the runner starts
------------------------
The applied files are re-read and their digests compared against the ones recorded
at application time.  If the tree no longer holds what was applied, verification
stops as ``BLOCKED``: running the suite would then measure a tree that is not the
one this workflow produced, and attributing the result to the fix would be wrong.

Failures, and what they mean
----------------------------
``FAILED``
    The runner exited non-zero.  The change is not verified.  The run ends in
    ``VERIFICATION_FAILED`` — a terminal *answer*, distinct from ``FAILED``,
    which means the workflow itself broke.
``TIMED_OUT``
    Stopped at the limit.  Nothing was concluded, and a timeout is never reported
    as a pass or as a test failure.
``BLOCKED``
    No runner started.  The run ends in ``FAILED``, because the workflow could
    not complete rather than having been told no.

The dividing line is one question, and it is queryable: *did a runner start?*
``tests_executed`` in the stored result answers it.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.analysis.fix_proposer import (
    SourceUnavailableError,
    UnsafeSourcePathError,
    resolve_safe_source_path,
)
from app.core.config import settings
from app.ingestion.cloner import IngestionError, LocalSource, RepositoryCloner
from app.models.domain import Evidence, EvidenceType
from app.models.fix_application import FixApplicationResult
from app.models.fix_proposal import FixChange, FixProposal
from app.models.test_plan import TestFileStatus, TestPlan, TestPlanStatus
from app.models.verification import (
    VerificationResult,
    VerificationStatus,
    blocked as blocked_result,
)
from app.models.workflow import StageResult, WorkflowState
from app.services.fix_application_stage import load_fix_application
from app.services.fix_applier import _replacement_lines, _with_terminator
from app.services.fix_proposal_stage import load_fix_proposal
from app.services.test_executor import (
    EXIT_NOTESTSCOLLECTED,
    EXIT_OK,
    ControlledTestExecutor,
    TestExecutionBlocked,
)
from app.services.stages import StageContext
from app.services.test_plan_stage import load_test_plan

logger = logging.getLogger(__name__)

#: ``run.context`` key holding the Phase 11 verification result.
VERIFICATION_KEY = "verification"

#: ``run.context`` key holding when verification ran.  Out of the model on the
#: Phase 8/9/10 reasoning.
VERIFICATION_AT_KEY = "verification_at"

#: ``agent`` name on the evidence this stage produces.
VERIFIER_AGENT = "verifier"

#: Every result carries this.  The executor is not a sandbox and must not be
#: described as one; see ``app.services.test_executor``.
_EXECUTION_SCOPE_LIMITATION = (
    "The test runner is a controlled invocation — a fixed, shell-free command "
    "with a scrubbed environment, a wall-clock limit and a captured output cap — "
    "not an operating-system sandbox. Running a Python suite necessarily executes "
    "the repository's own code, including its conftest.py."
)

_INTERPRETER_LIMITATION = (
    "The suite ran under this server's own Python interpreter, not the analysed "
    "repository's environment. A repository that needs additional packages may "
    "fail to collect, which is reported as a failure rather than a pass."
)

_NO_SOURCE_WRITTEN_LIMITATION = (
    "No test source was written by this tool. A green run therefore means the "
    "repository's pre-existing suite passes against the applied change, which is "
    "evidence against regression — not, on its own, evidence that the diagnosed "
    "defect is fixed. That stronger claim needs a test which discriminates the "
    "diagnosed behaviour from correct behaviour, and defect_demonstrated reports "
    "whether one was executed."
)


def load_verification(run) -> VerificationResult | None:
    """Return the verification result stored for ``run``, or None."""
    raw = run.context.get(VERIFICATION_KEY)
    if not isinstance(raw, dict):
        return None
    try:
        return VerificationResult.from_summary(run.run_id, raw)
    except Exception as exc:  # noqa: BLE001 - a bad summary must not crash a read
        logger.warning(
            "Verification for run %s could not be parsed: %s", run.run_id, exc
        )
        return None


def plan_targets(plan: TestPlan) -> list[str]:
    """Return the repository-relative test files the plan says already exist.

    Sorted and de-duplicated so the argument vector — and therefore the recorded
    command — is identical for two runs over the same plan.  Ordering is not a
    judgement about the tests; it is determinism.
    """
    return sorted(
        {
            scenario.suggested_file
            for scenario in plan.scenarios
            if scenario.file_status is TestFileStatus.EXISTING
            and scenario.suggested_file
        }
    )


def discriminating_targets(plan: TestPlan, executed: list[str]) -> list[str]:
    """Return the executed files the plan says discriminate the diagnosed defect.

    A *candidate* set, not an answer.  A scenario earns candidacy by carrying a
    ``discriminating_condition`` — Phase 10's record of what the input must be for
    a test to tell the diagnosed behaviour from correct behaviour — and by living
    in a file the runner actually executed.

    Candidacy is not proof, and nothing downstream treats it as such: the flag
    itself is decided by :func:`counterfactual_discrimination`.
    """
    ran = set(executed)
    return sorted(
        {
            scenario.suggested_file
            for scenario in plan.scenarios
            if scenario.suggested_file in ran and scenario.discriminating_condition
        }
    )


# ── Counterfactual discrimination ─────────────────────────────────────────────
#
# ``defect_demonstrated`` is the one field here that cannot be decided by reading
# the repository's *production* code, because the question is about the
# repository's *tests*.  It means: "a test that distinguishes the pre-image from
# the post-image was executed and passed".
#
# Two things that are not that, and which this module refuses to do:
#
# * "the suite is green" — a suite that asserts nothing about the changed
#   behaviour is green either way.  ShopDemo's own suite is the proof: its seven
#   tests pass on the broken code as well, so a green run there establishes
#   nothing whatsoever about the discount arithmetic.
# * "the test plan asked for a discriminating test" — the plan's
#   ``discriminating_condition`` describes a test that *should be written*.  It
#   is a prescription, not a receipt, and reading it as a receipt is how a demo
#   ends up claiming it proved something it did not.
#
# The honest answer is mechanical and already available: run *the same targets*
# against the tree with the applied change reverted, and see whether they fail.
# Failing before and passing after is the definition of discriminating, and it is
# the only available evidence that does not require this module to judge what a
# test asserts.
#
# Safety properties, each of which is a reason the revert happens in a scratch copy:
#
# * the analysed repository is never modified — the revert happens in a temporary
#   directory that is deleted afterwards;
# * the runner is the same :class:`ControlledTestExecutor`, so the second run has
#   the same fixed shell-free command, the same path validation, the same scrubbed
#   environment and the same wall-clock bound;
# * no test source is written, and no repository content is echoed into the
#   result, an event or the API — only the exit code is used;
# * every way this fails to decide yields ``False``, because a field that may only
#   be set on proof must default to "not proven".


def _revert_to_pre_image(scratch: Path, changes: list[FixChange]) -> None:
    """Undo ``changes`` inside ``scratch``, restoring each recorded ``before``.

    The exact inverse of the splice :meth:`app.services.fix_applier.FixApplier`
    performed, reusing its terminator helpers so a CRLF file stays CRLF and a file
    whose last line has no newline keeps having none.

    Raises :class:`ValueError` if the recorded post-image is not where the
    proposal said it would be.  That is not recoverable and must not be guessed
    around: a pre-image that is not the real pre-image would answer the
    counterfactual with fiction.
    """
    by_relative: dict[str, list[FixChange]] = {}
    for change in changes:
        by_relative.setdefault(change.file_path, []).append(change)

    for relative, file_changes in by_relative.items():
        try:
            path = resolve_safe_source_path(scratch, relative)
        except (UnsafeSourcePathError, SourceUnavailableError) as exc:
            raise ValueError(f"cannot resolve {relative} in the scratch copy: {exc}")
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)

        # Descending line order, so an earlier splice cannot shift a later range.
        for change in sorted(file_changes, key=lambda c: c.line, reverse=True):
            start = change.line - 1
            stop = change.line_end or change.line
            if start < 0 or stop > len(lines) or start >= stop:
                raise ValueError(
                    f"line {change.line} of {relative} is outside the scratch copy"
                )
            exemplar = lines[stop - 1]
            if "".join(lines[start:stop]) != "".join(
                _replacement_lines(change.after, exemplar)
            ):
                raise ValueError(
                    f"line {change.line} of {relative} does not hold the post-image"
                )
            lines[start:stop] = _replacement_lines(change.before, exemplar)

        path.write_text("".join(lines), encoding="utf-8")


def counterfactual_discrimination(
    executor: ControlledTestExecutor,
    *,
    root: Path,
    targets: list[str],
    proposal: FixProposal | None,
    timeout_seconds: float,
) -> tuple[bool, str]:
    """Do ``targets`` distinguish the pre-image from the post-image?

    Runs the same test files against a scratch copy of ``root`` with every
    recorded change reverted, and returns ``(discriminates, reason)``.

    ``discriminates`` is True only when that reverted run produced a real non-zero
    exit code.  A runner that could not start, a timeout, a copy that could not be
    built and a pre-image that could not be restored all mean *not decided*, and
    every one of them returns ``False`` with a reason saying so.
    """
    if proposal is None or not proposal.changes:
        return False, (
            "No recorded change is available to revert, so whether the executed "
            "tests distinguish the diagnosed behaviour was not decided."
        )

    scratch_parent = Path(tempfile.mkdtemp(prefix="mentor-preimage-"))
    scratch = scratch_parent / "repo"
    try:
        try:
            shutil.copytree(
                root,
                scratch,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git"),
            )
        except OSError as exc:
            logger.warning("Could not build a pre-image copy of %s: %s", root, exc)
            return False, (
                "A copy of the repository with the change reverted could not be "
                "made, so whether the executed tests distinguish the diagnosed "
                "behaviour was not decided."
            )

        try:
            _revert_to_pre_image(scratch, proposal.changes)
        except (ValueError, OSError, UnicodeDecodeError) as exc:
            logger.warning("Could not restore the pre-image of %s: %s", root, exc)
            return False, (
                f"The pre-image this proposal was built against could not be "
                f"restored for the counterfactual run ({exc}), so whether the "
                f"executed tests distinguish the diagnosed behaviour was not "
                f"decided."
            )

        try:
            reverted = executor.run(
                root=scratch, targets=targets, timeout_seconds=timeout_seconds
            )
        except TestExecutionBlocked as exc:
            logger.info("Counterfactual pre-image run was blocked: %s", exc)
            return False, (
                f"The counterfactual run against the pre-image could not be "
                f"attempted ({exc}), so whether the executed tests distinguish "
                f"the diagnosed behaviour was not decided."
            )
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception("Counterfactual pre-image run failed")
            return False, (
                f"The counterfactual run against the pre-image raised "
                f"{type(exc).__name__}, so whether the executed tests "
                f"distinguish the diagnosed behaviour was not decided."
            )

        if reverted.timed_out:
            return False, (
                "The counterfactual run against the pre-image was stopped at the "
                "timeout, so whether the executed tests distinguish the diagnosed "
                "behaviour was not decided. A timeout is not a discrimination."
            )
        if reverted.exit_code == EXIT_OK:
            return False, (
                "The same tests also pass against the tree with the change "
                "reverted, so they do not distinguish the diagnosed behaviour "
                "from correct behaviour. This pass establishes the absence of "
                "regression and nothing about the defect itself."
            )
        return True, (
            f"The same tests fail against the tree with the change reverted "
            f"(exit code {reverted.exit_code}) and pass against the applied "
            f"change, so a test that distinguishes the diagnosed behaviour from "
            f"correct behaviour was executed and passed."
        )
    finally:
        shutil.rmtree(scratch_parent, ignore_errors=True)


# ── The stage ─────────────────────────────────────────────────────────────────


class VerificationStage:
    """Runs the existing suite and records what it established. ``VERIFICATION``.

    Registered against ``VERIFICATION`` and declares the same state as its
    ``next_state``: a successful verification *is* the run being in
    ``VERIFICATION`` with a recorded result.  ``VERIFIED`` is entered by the
    orchestrator's ``complete_run`` only after a result with a zero exit code
    exists, so the transition to ``VERIFIED`` cannot be reached by anything but a
    real execution.
    """

    state = WorkflowState.VERIFICATION

    #: Not a pytest class; see ``TestPlanStatus`` for the same convention.
    __test__ = False

    def __init__(
        self,
        executor: ControlledTestExecutor | None = None,
        cloner: RepositoryCloner | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._executor = executor or ControlledTestExecutor(
            default_timeout_seconds=settings.verification_timeout_seconds,
            max_targets=settings.verification_max_targets,
        )
        self._cloner = cloner or RepositoryCloner()
        self._timeout = (
            settings.verification_timeout_seconds
            if timeout_seconds is None
            else timeout_seconds
        )

    def execute(self, context: StageContext) -> StageResult:
        run = context.run
        emit = context.emit

        if emit is not None:
            emit(
                "VERIFICATION_STARTED",
                (
                    f"Verifying run {run.run_id} by running the repository's "
                    f"existing test files. No test source is written, and the "
                    f"runner is invoked with a fixed, shell-free command under a "
                    f"{self._timeout:g}s limit."
                ),
                {
                    "run_id": run.run_id,
                    "timeout_seconds": self._timeout,
                },
            )

        plan = load_test_plan(run)
        if plan is None:
            return self._fail(
                emit,
                run.run_id,
                VERIFICATION_KEY,
                None,
                "VERIFICATION cannot run: no test plan is recorded for this run.",
            )
        if plan.status is not TestPlanStatus.READY_FOR_REVIEW:
            return self._fail(
                emit,
                run.run_id,
                VERIFICATION_KEY,
                None,
                f"VERIFICATION cannot run: the recorded test plan has status "
                f"{plan.status.value} and outlines no scenario to execute. "
                f"{plan.rationale}",
            )

        application = load_fix_application(run)
        if application is None or not application.applied():
            return self._fail(
                emit,
                run.run_id,
                VERIFICATION_KEY,
                None,
                "VERIFICATION cannot run: no applied fix is recorded, so there is "
                "no change to verify against a test run.",
            )

        try:
            root = self._cloner.prepare(LocalSource(path=run.repository_path))
        except IngestionError as exc:
            return self._fail(emit, run.run_id, VERIFICATION_KEY, application, exc.reason)
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception("Verification could not resolve the repository")
            return self._fail(
                emit,
                run.run_id,
                VERIFICATION_KEY,
                application,
                f"{type(exc).__name__}: {exc}",
            )

        # ── Is the tree still what we applied? ───────────────────────────
        current, drift = self._recheck(root, application)
        if drift:
            result = blocked_result(
                run.run_id,
                (
                    f"Verification was not attempted because the working tree no "
                    f"longer holds the applied change: {', '.join(drift)} differs "
                    f"from the post-image recorded when the fix was applied."
                ),
                limitation=(
                    "No test was executed, so nothing about this repository has "
                    "been verified. The recorded application cannot be trusted as "
                    "a description of the current tree."
                ),
                proposal_fingerprint=application.proposal_fingerprint,
                verified_file_hashes=current,
                fix_applied=False,
            )
            return self._record(emit, run.run_id, result, outcome_failed=True)

        targets = plan_targets(plan)
        if not targets:
            result = blocked_result(
                run.run_id,
                (
                    f"Verification was not attempted because none of the "
                    f"{len(plan.scenarios)} scenario(s) in the test plan target a "
                    f"test file that already exists in this repository. Every "
                    f"scenario names a new file, and this tool writes no test "
                    f"source."
                ),
                limitation=(
                    "No test was executed, so nothing has been verified. The plan "
                    "is a proposal to write tests, not a test run."
                ),
                proposal_fingerprint=application.proposal_fingerprint,
                verified_file_hashes=current,
                fix_applied=True,
            )
            return self._record(emit, run.run_id, result, outcome_failed=True)

        # ── Run ──────────────────────────────────────────────────────────
        try:
            run_result = self._executor.run(
                root=root,
                targets=targets,
                timeout_seconds=self._timeout,
            )
        except TestExecutionBlocked as exc:
            result = blocked_result(
                run.run_id,
                f"Verification was not attempted: {exc}",
                limitation=(
                    "No test was executed, so nothing has been verified."
                ),
                proposal_fingerprint=application.proposal_fingerprint,
                verified_file_hashes=current,
                fix_applied=True,
            )
            return self._record(emit, run.run_id, result, outcome_failed=True)
        except Exception as exc:  # noqa: BLE001 - reported, never silently ignored
            logger.exception("Verification runner failed for run %s", run.run_id)
            return self._fail(
                emit,
                run.run_id,
                VERIFICATION_KEY,
                application,
                f"{type(exc).__name__}: {exc}",
            )

        result = self._interpret(
            run_id=run.run_id,
            plan=plan,
            application=application,
            current=current,
            run_result=run_result,
            root=root,
            proposal=load_fix_proposal(run),
        )
        # A verdict — in either direction — is a completed stage.  A completed
        # stage moves the run to VERIFICATION, and the orchestrator then either
        # completes it (PASSED) or moves it to VERIFICATION_FAILED.
        return self._record(emit, run.run_id, result, outcome_failed=False)

    # ── Interpretation ───────────────────────────────────────────────────

    def _interpret(
        self,
        *,
        run_id: str,
        plan: TestPlan,
        application: FixApplicationResult,
        current: dict[str, str],
        run_result,
        root: Path,
        proposal: FixProposal | None,
    ) -> VerificationResult:
        """Turn a finished runner into a verdict, or into a block.

        The only branch that produces ``PASSED`` is the one where the runner
        exited zero.  Everything else — a non-zero code, a timeout, a collection
        that found nothing — produces a status that is not a pass, and says in
        ``findings`` what was actually observed.
        """
        limitations = [
            _EXECUTION_SCOPE_LIMITATION,
            _INTERPRETER_LIMITATION,
            _NO_SOURCE_WRITTEN_LIMITATION,
        ]
        if run_result.output_truncated:
            limitations.append(
                "The runner's output exceeded the capture limit and was "
                "truncated in this record; the full output is not retained."
            )

        target_list = list(run_result.targets)

        if run_result.timed_out:
            return VerificationResult(
                run_id=run_id,
                status=VerificationStatus.TIMED_OUT,
                test_run=run_result,
                targets=target_list,
                verified_file_hashes=current,
                proposal_fingerprint=application.proposal_fingerprint,
                tests_executed=True,
                regression_check=False,
                defect_demonstrated=False,
                fix_applied=True,
                findings=[
                    f"The runner was stopped at the {self._timeout:g}s limit after "
                    f"being pointed at {len(target_list)} existing test file(s). "
                    f"No verdict was reached about the change: a timeout is not a "
                    f"pass and not a test failure."
                ],
                limitations=limitations,
            )

        if run_result.exit_code == EXIT_NOTESTSCOLLECTED:
            return blocked_result(
                run_id,
                (
                    f"The runner collected no tests from "
                    f"{', '.join(target_list)}. The files exist and the plan names "
                    f"them, but nothing in them was collectable as a test."
                ),
                limitation=(
                    "No test was executed, so nothing has been verified. The "
                    "test plan's file selection and the repository's collection "
                    "rules disagree, and this tool does not guess which is wrong."
                ),
                proposal_fingerprint=application.proposal_fingerprint,
                verified_file_hashes=current,
                fix_applied=True,
            )

        if run_result.exit_code != EXIT_OK:
            return VerificationResult(
                run_id=run_id,
                status=VerificationStatus.FAILED,
                test_run=run_result,
                targets=target_list,
                verified_file_hashes=current,
                proposal_fingerprint=application.proposal_fingerprint,
                tests_executed=True,
                regression_check=False,
                defect_demonstrated=False,
                fix_applied=True,
                findings=[
                    f"The repository's existing suite reported failures against "
                    f"the applied change: exit code {run_result.exit_code}, "
                    f"{_counts_phrase(run_result)} across "
                    f"{len(target_list)} file(s). The fix is not verified. Which "
                    f"assertions failed, and whether they failed because of this "
                    f"change, is not decided here — the captured output is in this "
                    f"result."
                ],
                limitations=[
                    "A failing suite establishes that the change is not verified. "
                    "It does not establish that the change is the cause: the "
                    "suite may have been failing beforehand.",
                    *limitations,
                ],
            )

        # ── Exit code zero. The only path to PASSED. ──────────────────────
        # Whether that pass *demonstrates the defect* is a second, independent
        # question, and it is answered by a counterfactual run rather than by
        # anything this module can read.  A green suite is not enough: the same
        # suite is green on the broken code whenever it never asserts the changed
        # behaviour.  So the identical targets are re-run against a scratch copy
        # with the change reverted, and only a real failure there counts.
        demonstrated, discrimination = counterfactual_discrimination(
            self._executor,
            root=root,
            targets=target_list,
            proposal=proposal,
            timeout_seconds=self._timeout,
        )
        return VerificationResult(
            run_id=run_id,
            status=VerificationStatus.PASSED,
            test_run=run_result,
            targets=target_list,
            verified_file_hashes=current,
            proposal_fingerprint=application.proposal_fingerprint,
            tests_executed=True,
            regression_check=True,
            defect_demonstrated=demonstrated,
            fix_applied=True,
            findings=[
                f"The repository's existing suite passed against the applied "
                f"change: exit code 0, {_counts_phrase(run_result)} across "
                f"{len(target_list)} file(s). The change introduced no regression "
                f"in the tests this repository already had.",
                "The applied files were re-read immediately before the run and "
                "hold the same bytes that were recorded when the fix was applied, "
                "so this result is about the change this run made.",
                discrimination,
            ],
            limitations=limitations,
        )

    # ── Re-check ─────────────────────────────────────────────────────────

    @staticmethod
    def _recheck(
        root: Path, application: FixApplicationResult
    ) -> tuple[dict[str, str], list[str]]:
        """Re-read the applied files and compare against the application record.

        Returns ``(digests_by_path, drifted_paths)``.  Drift is not an error to be
        raised — the caller turns it into ``BLOCKED``, because running the suite
        against a tree that is not the one this workflow produced would attribute
        a result to the wrong thing.
        """
        from app.analysis.fix_proposer import (
            SourceUnavailableError,
            UnsafeSourcePathError,
            resolve_safe_source_path,
        )

        digests: dict[str, str] = {}
        drift: list[str] = []
        for entry in application.files:
            try:
                data = resolve_safe_source_path(root, entry.file_path).read_bytes()
            except (UnsafeSourcePathError, SourceUnavailableError, OSError):
                drift.append(entry.file_path)
                continue
            digest = hashlib.sha256(data).hexdigest()
            digests[entry.file_path] = digest
            if digest != entry.after_sha256:
                drift.append(entry.file_path)
        return digests, drift

    # ── Recording ────────────────────────────────────────────────────────

    def _record(
        self,
        emit,
        run_id: str,
        result: VerificationResult,
        *,
        outcome_failed: bool,
    ) -> StageResult:
        """Persist ``result`` and shape the stage outcome around it.

        ``outcome_failed`` says whether the *workflow* could continue, not whether
        the change was verified:

        * ``False`` for a verdict (``PASSED``, ``FAILED``, ``TIMED_OUT``) — the
          stage ran and produced an answer, which is a completed stage.
        * ``True`` for ``BLOCKED`` and for a stage that could not run at all —
          nothing was concluded, so the run must not be treated as though it were
          progressing.
        """
        verified_at = datetime.now(timezone.utc).isoformat()
        payload = {"run_id": run_id, **result.as_event_payload()}

        if emit is not None:
            if result.status is VerificationStatus.BLOCKED:
                emit(
                    "VERIFICATION_FAILED",
                    (
                        f"Verification could not reach a verdict for run "
                        f"{run_id}: {result.findings[0] if result.findings else ''}"
                    ),
                    {**payload, "blocked": True},
                )
            else:
                emit(
                    "VERIFICATION_COMPLETED",
                    f"Verification finished for run {run_id} with status "
                    f"{result.status.value}.",
                    payload,
                )

        data = {
            VERIFICATION_KEY: result.to_summary(),
            VERIFICATION_AT_KEY: verified_at,
            "verification_status": result.status.value,
            "verification_tests_executed": result.tests_executed,
            "verification_regression_check": result.regression_check,
            "verification_defect_demonstrated": result.defect_demonstrated,
            "verified_result_count": 1 if result.status is VerificationStatus.PASSED else 0,
        }

        if outcome_failed:
            reason = (
                result.findings[0]
                if result.findings
                else "Verification could not be completed."
            )
            return StageResult.failed(
                "The change could not be verified.", reason
            )

        message = (
            f"Verification {result.status.value}: "
            f"{result.findings[0] if result.findings else 'no detail recorded'}"
        )
        return StageResult.completed(
            message,
            next_state=self.state,
            evidence=_verification_evidence(run_id, result),
            **data,
        )

    def _fail(
        self,
        emit,
        run_id: str,
        key: str,
        application: FixApplicationResult | None,
        error: str,
    ) -> StageResult:
        """Report a stage-level failure: the workflow broke, not the change."""
        if emit is not None:
            emit(
                "VERIFICATION_FAILED",
                f"Verification failed for run {run_id}: {error}",
                {"run_id": run_id, "error": error},
            )
        logger.warning("Verification stage failed for run %s: %s", run_id, error)
        return StageResult.failed("The change could not be verified.", error)


# ── Evidence ──────────────────────────────────────────────────────────────────


def _verification_evidence(run_id: str, result: VerificationResult) -> list[Evidence]:
    """At most one evidence item: a ``VERIFIED_RESULT`` only on a real pass.

    The type system carries the rule.  ``EvidenceType.VERIFIED_RESULT`` may only
    be attached here, and only for ``status is PASSED``, which may only exist
    alongside a runner that exited zero.  Every other outcome records nothing at
    all: a failed or blocked run is reported through the workflow state and the
    stored result, and adding an ``OBSERVATION`` about it would put a fact about
    the *tool* into a stream that a client reads as findings about the
    *repository*.
    """
    if result.status is not VerificationStatus.PASSED:
        return []
    return [
        Evidence(
            session_id=run_id,
            evidence_type=EvidenceType.VERIFIED_RESULT,
            agent=VERIFIER_AGENT,
            title=(
                f"Repository suite passed against the applied change "
                f"(exit code 0, {result.test_run.tests_run if result.test_run.tests_run is not None else 'unreported'} "
                f"test(s) across {len(result.targets)} file(s))"
            ),
            description=(
                f"After explicit human approval, the approved change was applied "
                f"and confirmed on disk, then the repository's pre-existing test "
                f"file(s) {', '.join(result.targets)} were run by the controlled "
                f"executor and the runner exited 0 in "
                f"{result.test_run.duration_seconds:g}s. This is a VERIFIED_RESULT: "
                f"it rests on that exit code and nothing else. It establishes that "
                f"the applied change did not regress the suite the repository "
                f"already had. It does not establish that the diagnosed defect is "
                f"fixed — no test discriminating the diagnosed behaviour was "
                f"executed, and this tool wrote no test source."
            ),
            source_file=result.targets[0] if result.targets else None,
            confidence=None,
            timestamp=datetime.now(timezone.utc),
            metadata={
                "investigator": VERIFIER_AGENT,
                "method": "controlled_test_execution",
                "phase": 11,
                "status": result.status.value,
                "exit_code": result.test_run.exit_code,
                "targets": list(result.targets),
                "proposal_fingerprint": result.proposal_fingerprint,
                "regression_check": True,
                "defect_demonstrated": False,
                "test_source_written": False,
            },
        )
    ]


def _counts_phrase(run_result) -> str:
    """A short, honest description of the runner's own counts."""
    if run_result.tests_run is None:
        return "an unreported number of tests"
    return (
        f"{run_result.tests_run} test(s) run, {run_result.passed} passed, "
        f"{run_result.failed} failed"
    )


__all__ = [
    "VERIFICATION_AT_KEY",
    "VERIFICATION_KEY",
    "VERIFIER_AGENT",
    "VerificationStage",
    "counterfactual_discrimination",
    "discriminating_targets",
    "load_verification",
    "plan_targets",
]
