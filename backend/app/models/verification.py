"""
Verification domain types — Phase 11 (executed tests and what they established).

The *domain* half of the verify path.  Pure Pydantic models, no I/O, no clock and
no LLM dependency, following the same convention as the rest of ``app.models``.

This is the first model in the project that may carry a ``VERIFIED_RESULT``
indirectly, so it is also the first one whose shape has to make lying hard.

Why ``PASSED`` cannot be asserted
---------------------------------
AGENTS.md rule 4 says a verified result comes only from a real exit code, and this
module enforces that *in the type* rather than in a code review:

* :class:`VerificationStatus` has no ``PASSED``-like member that a caller can set
  by hand.  ``PASSED`` is only constructible together with a
  :class:`TestRunResult` whose ``exit_code`` is ``0`` and whose ``timed_out`` is
  ``False`` — a :func:`~app.models.verification.verify` model validator rejects
  every other combination.  So "the tests passed" and "the runner exited zero" are
  not two fields that can drift apart, because there is only one way to hold both.
* A blocked run — no runnable test target, the runner could not start, the
  repository is gone — is ``BLOCKED``, which is neither a pass nor a failure.  It
  is the answer this project needs most often and the one most easily lost, so it
  gets its own member instead of being folded into ``FAILED``.
* ``NOT_RUN`` is kept for the case where verification has not been attempted at
  all, so a read endpoint can distinguish "not attempted" from "attempted and
  blocked".  It is never a terminal verification outcome.

What a pass does and does not mean
---------------------------------
Phase 11 writes **no test source**.  The runner executes test files that already
existed in the analysed repository.  So a ``PASSED`` status means *"the
repository's own suite, as it stands, passes against the applied change"* — which
is real evidence that the change introduced no regression, and is **not** by
itself evidence that the diagnosed defect is fixed, unless one of the executed
files actually exercises the changed code path.

That distinction is not a caveat bolted on afterwards; it is the difference between
the two ``limitations`` entries the model carries on every result, and the
``regression_check`` / ``defect_demonstrated`` fields below make it queryable
rather than buried in prose.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field, model_validator

#: Upper bound on captured stdout/stderr retained in a result.  Test output is
#: unbounded and frequently enormous; a stored result must not become a
#: log-storage problem or a way to push megabytes through the API.
MAX_CAPTURED_OUTPUT_CHARS = 8000

#: Upper bound on a single output excerpt line.  One pathological line (a giant
#: repr, a minified blob) should not defeat the cap above.
MAX_CAPTURED_LINE_CHARS = 500


# ── Status ────────────────────────────────────────────────────────────────────


class VerificationStatus(str, Enum):
    """The outcome of one verification attempt.

    ``PASSED``
        The runner exited ``0``.  Constructible only with a matching
        :class:`TestRunResult` — see the module docstring.
    ``FAILED``
        The runner ran and reported failing tests (a non-zero exit code that was
        not a timeout).  The fix is *not* verified, and saying so is a real,
        useful answer: the suite that was expected to pass did not.
    ``TIMED_OUT``
        The runner was stopped at the configured limit.  Distinct from
        ``FAILED``: nothing was concluded about the change, and a timeout must
        never be reported as a test failure or as a pass.
    ``BLOCKED``
        Verification could not be attempted: no runnable test target exists, the
        runner could not be started, or the repository is no longer resolvable.
        Not a failure of the fix, and not a pass.
    ``NOT_RUN``
        Verification was never attempted for this run.
    """

    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    BLOCKED = "BLOCKED"
    NOT_RUN = "NOT_RUN"

    #: Not a pytest class — see ``TestPlanStatus`` for the same convention.
    __test__ = False

    @property
    def is_conclusive(self) -> bool:
        """True when the run actually reached a verdict about the change.

        ``PASSED`` and ``FAILED`` are verdicts.  ``TIMED_OUT``, ``BLOCKED`` and
        ``NOT_RUN`` are the absence of one, and the workflow treats them
        differently: a ``FAILED`` is a terminal, honest answer, while the others
        mean the answer is still owed.
        """
        return self in (VerificationStatus.PASSED, VerificationStatus.FAILED)


# ── The executed run ──────────────────────────────────────────────────────────


class TestRunResult(BaseModel):
    """What the controlled runner actually did, and what it reported back.

    ``command`` is the fully-rendered argument vector, not a shell string.  It is
    built by the executor from a fixed prefix and a server-derived file list; no
    part of it is ever taken from a request, a proposal or a model.  Recording it
    verbatim is what makes an execution auditable after the fact.

    ``tests_run``/``passed``/``failed`` are parsed from the runner's own summary
    line when one is present and stay ``None`` when it is not.  A ``None`` count
    with ``exit_code=0`` is honest — "the runner succeeded and did not print a
    parseable count" — and is not the same claim as a count of zero.
    """

    #: The exact argument vector passed to the runner, shell-free.
    command: list[str] = Field(default_factory=list)
    #: The runner's exit status.  ``None`` only when it never started.
    exit_code: int | None = None
    duration_seconds: float = 0.0
    #: True when the runner was stopped at the timeout.  ``exit_code`` is then
    #: meaningless as a verdict and ``timed_out`` is authoritative.
    timed_out: bool = False
    #: Bounded excerpt of the runner's stdout.
    stdout_excerpt: str = ""
    #: Bounded excerpt of the runner's stderr.
    stderr_excerpt: str = ""
    #: True when either excerpt was cut to fit.  Reported so a truncated log is
    #: visible as truncated rather than being read as the whole output.
    output_truncated: bool = False
    #: Test targets that were named, in the order they were passed to the runner.
    targets: list[str] = Field(default_factory=list)
    #: How many targets were requested.  Kept separate from ``targets`` so a
    #: truncated request is visible rather than invisible.
    tests_requested: int = 0
    tests_run: int | None = None
    passed: int | None = None
    failed: int | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def started(self) -> bool:
        """True when this run represents a runner that actually started.

        Deliberately inferred from what a started run must carry — a rendered
        command and at least one named target — rather than from a flag someone
        could set.  A ``TestRunResult`` with no exit code *and* no command is the
        "never started" shape, and that is the only way to get one.

        Serialized because a client cannot tell the two shapes apart otherwise:
        ``exit_code=None`` reads as "the runner succeeded silently" unless the
        record also states that nothing ran.
        """
        return bool(self.command) and bool(self.targets)

    @model_validator(mode="after")
    def _a_started_run_has_an_exit_code(self) -> "TestRunResult":
        if self.duration_seconds < 0:
            raise ValueError("duration_seconds must not be negative")
        if self.timed_out:
            if self.exit_code not in (None, 0):
                raise ValueError(
                    "a timed-out run has no meaningful exit code; the timeout is "
                    "the outcome"
                )
                if not self.started:
                    raise ValueError(
                        "a timed-out run must have started; a run that never began "
                        "cannot be stopped by a timeout"
                    )
            return self
        if self.started and self.exit_code is None:
            raise ValueError(
                "a run that started must carry the runner's exit code; None is "
                "reserved for the never-started shape, which belongs in the "
                "verification status as BLOCKED"
            )
        if self.exit_code is not None and not self.started:
            raise ValueError(
                "an exit code implies a runner started, so the run must carry a "
                "command and a target"
            )
        return self


# ── The result ────────────────────────────────────────────────────────────────


class VerificationResult(BaseModel):
    """What one verification attempt established about one run.

    The four status fields are deliberately separate booleans rather than a
    summary sentence, because they are four different claims and conflating them
    is how a demo ends up saying it proved something it did not:

    ``tests_executed``
        A runner actually started.  False for ``BLOCKED`` and ``NOT_RUN``.
    ``regression_check``
        The repository's own suite was executed against the applied change and
        reported success.  This is the strongest claim Phase 11 can make, and it
        is about *absence of regression*, not about the diagnosed defect.
    ``defect_demonstrated``
        A test that discriminates the diagnosed behaviour from correct
        behaviour was executed and passed.  Phase 11 writes no test source, so
        this is True only when a pre-existing test in the repository already
        discriminates.  On the demo repository it is False, and that is a real,
        reportable fact rather than a gap in the implementation.
    ``fix_applied``
        The repository currently holds the approved change.  Recorded here as
        well as on the application result so a reader of *only* this record can
        tell whether it verified a fix or an unfixed tree.
    """

    run_id: str
    status: VerificationStatus
    test_run: TestRunResult = Field(default_factory=TestRunResult)
    #: Repository-relative paths the runner was pointed at.
    targets: list[str] = Field(default_factory=list)
    #: SHA-256 of every applied file at verification time, re-read from disk, so
    #: this record is bound to the exact bytes that were tested.
    verified_file_hashes: dict[str, str] = Field(default_factory=dict)
    #: The fingerprint of the proposal that was applied and tested.
    proposal_fingerprint: str = ""

    tests_executed: bool = False
    regression_check: bool = False
    defect_demonstrated: bool = False
    fix_applied: bool = False

    #: Plain statements of what was established.  Reported, not generated.
    findings: list[str] = Field(default_factory=list)
    #: What this result does *not* establish.  Never empty: even a clean pass has
    #: a scope, and an empty list would invite a reader to assume there is none.
    limitations: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _claims_must_agree_with_the_status(self) -> "VerificationResult":
        if self.status is VerificationStatus.PASSED:
            if self.test_run.exit_code != 0 or self.test_run.timed_out:
                raise ValueError(
                    "PASSED requires a runner that exited 0 and was not stopped "
                    "by the timeout; no other combination may claim a pass"
                )
            if not self.tests_executed:
                raise ValueError(
                    "PASSED requires tests_executed=True: the runner must have "
                    "started"
                )
            if not self.regression_check:
                raise ValueError(
                    "PASSED is a regression check, so regression_check must be "
                    "True whenever the status is PASSED"
                )
        if self.status in (
            VerificationStatus.BLOCKED,
            VerificationStatus.NOT_RUN,
        ):
            if self.tests_executed or self.regression_check:
                raise ValueError(
                    f"status {self.status.value} means no runner started, so "
                    f"tests_executed and regression_check must both be False"
                )
        if self.regression_check and self.status is not VerificationStatus.PASSED:
            raise ValueError(
                "a regression check can only have succeeded, so it belongs with "
                "status PASSED; on any other status it would be claiming a pass "
                "that did not happen"
            )
        if self.defect_demonstrated and self.status is not VerificationStatus.PASSED:
            raise ValueError(
                "defect_demonstrated requires a successful execution, so the "
                "status must be PASSED"
            )
        if not self.limitations:
            raise ValueError(
                "every verification result must state what it does not establish; "
                "an empty limitations list would invite a reader to assume there "
                "is no scope limit"
            )
        return self

    def as_event_payload(self) -> dict[str, Any]:
        """Payload for the ``VERIFICATION_*`` events.

        Counts, status and paths only.  Captured output is deliberately excluded:
        a test runner's stdout routinely contains the values under test and the
        source line that produced them, and events are broadcast to every
        connected client.
        """
        return {
            "status": self.status.value,
            "tests_executed": self.tests_executed,
            "regression_check": self.regression_check,
            "defect_demonstrated": self.defect_demonstrated,
            "fix_applied": self.fix_applied,
            "exit_code": self.test_run.exit_code,
            "timed_out": self.test_run.timed_out,
            "duration_seconds": self.test_run.duration_seconds,
            "targets": list(self.targets),
            "tests_requested": self.test_run.tests_requested,
            "tests_run": self.test_run.tests_run,
            "passed": self.test_run.passed,
            "failed": self.test_run.failed,
            "proposal_fingerprint": self.proposal_fingerprint,
        }

    # ── Persistence helpers ─────────────────────────────────────────────

    def to_summary(self) -> dict[str, Any]:
        """Serialise the result for ``run.context``."""
        return self.model_dump(mode="json")

    @classmethod
    def from_summary(
        cls, run_id: str, summary: dict[str, Any]
    ) -> "VerificationResult":
        """Rebuild a result previously written by :meth:`to_summary`."""
        data = dict(summary)
        data["run_id"] = run_id
        return cls.model_validate(data)


# ── Construction helpers ──────────────────────────────────────────────────────


def blocked(
    run_id: str,
    reason: str,
    *,
    limitation: str,
    proposal_fingerprint: str = "",
    verified_file_hashes: dict[str, str] | None = None,
    fix_applied: bool = False,
) -> VerificationResult:
    """Build a ``BLOCKED`` result — verification could not be attempted.

    Used for every "there is nothing to run" case: no test target in the plan,
    the runner could not start, the repository is gone.  All of them are the same
    *kind* of answer — the verdict is still owed — so they share one constructor
    and cannot be accidentally reported as a failure.
    """
    return VerificationResult(
        run_id=run_id,
        status=VerificationStatus.BLOCKED,
        test_run=TestRunResult(),
        targets=[],
        verified_file_hashes=dict(verified_file_hashes or {}),
        proposal_fingerprint=proposal_fingerprint,
        tests_executed=False,
        regression_check=False,
        defect_demonstrated=False,
        fix_applied=fix_applied,
        findings=[reason],
        limitations=[limitation],
    )


def not_run(run_id: str, reason: str) -> VerificationResult:
    """Build a ``NOT_RUN`` result — verification was never attempted."""
    return VerificationResult(
        run_id=run_id,
        status=VerificationStatus.NOT_RUN,
        test_run=TestRunResult(),
        findings=[reason],
        limitations=[
            "No test was executed for this run, so nothing about the repository "
            "has been verified."
        ],
    )


__all__ = [
    "MAX_CAPTURED_LINE_CHARS",
    "MAX_CAPTURED_OUTPUT_CHARS",
    "TestRunResult",
    "VerificationResult",
    "VerificationStatus",
    "blocked",
    "not_run",
]
