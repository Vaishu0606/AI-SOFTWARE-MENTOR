"""
Workflow domain types — Phase 6 (Orchestrator, state machine, SSE).

These types are the *domain* half of the Phase 6 orchestrator.  They are pure
Pydantic models and are intentionally decoupled from persistence
(``app.models.schemas``) and transport (Pydantic HTTP schemas in ``app.api``),
matching the convention established by ``app.models.domain``.

Design constraints
------------------
* DETERMINISTIC — no LLM, no randomness, no clock-dependent branching.  The
  state machine lives in ``app.services.state_machine``.
* HONEST — every stage reports only what it did.  Phases 7–10 could refuse to
  touch a repository because none of them had the capability; Phase 11 has it,
  and is careful to distinguish "refused" from "declined" and "verified" from
  "did not find a failure".  Placeholder stages report
  ``StageOutcome.PENDING`` and return **zero** evidence items so that a
  placeholder can never be mistaken for a real finding.
* SERVER-OWNED — ``current_state`` and ``status`` are written by the
  orchestrator only.  No client-supplied value can influence them.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator

from app.models.domain import Evidence


def utc_now() -> datetime:
    """Return the current UTC timestamp (single source of truth for the run)."""
    return datetime.now(timezone.utc)


def new_run_id() -> str:
    """Generate a fresh, unique workflow run identifier."""
    return str(uuid.uuid4())


def new_event_id() -> str:
    """Generate a fresh, unique workflow event identifier."""
    return str(uuid.uuid4())


# ── Run identifiers ───────────────────────────────────────────────────────────

#: Run IDs are server-generated UUID4 strings.  Anything else is rejected
#: before it ever reaches the persistence layer.
RUN_ID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

MAX_RUN_ID_LENGTH = 36
MAX_REPOSITORY_PATH_LENGTH = 1024
MAX_ISSUE_LENGTH = 8000


def is_valid_run_id(value: str) -> bool:
    """Return True when ``value`` is a well-formed, server-issued run ID."""
    if not isinstance(value, str) or not value:
        return False
    if len(value) > MAX_RUN_ID_LENGTH:
        return False
    return RUN_ID_PATTERN.match(value) is not None


# ── Workflow state ────────────────────────────────────────────────────────────


class WorkflowState(str, Enum):
    """The state of a mentor workflow run.

    ``CREATED`` → ``REPOSITORY_CONTEXT`` → ``ARCHITECTURE_CONTEXT`` are backed
    by real Phase 3 / Phase 5 analysis, ``INVESTIGATION_PENDING`` by Phase 7,
    ``ROOT_CAUSE_ANALYSIS`` by Phase 8, ``FIX_PROPOSAL`` by Phase 9,
    ``TEST_PLAN`` by Phase 10, and ``FIX_APPLYING`` / ``VERIFICATION`` by
    Phase 11.

    A state name describes work that has *happened*, not work that is planned.
    ``ROOT_CAUSE_READY`` is the hand-off from Phase 8 into Phase 9, and
    ``FIX_PROPOSAL_READY`` is the Phase 9 boundary: a fix proposal has been
    drafted and is waiting for a human to review it.  ``FIX_PENDING`` was the
    original placeholder name for this work and is replaced by the two states
    above, because a proposed fix and a fix awaiting review are different
    things.  ``TEST_PENDING`` was replaced the same way: the orchestrator reserves
    the ``*_PENDING`` suffix for a stage that is not implemented, and Phase 10
    runs a real one.  It is ``TEST_PLAN`` rather than ``TEST`` because Phase 10
    outlines tests in prose and writes no test source, so the name must not imply
    a test file exists.

    Phase 11 renames ``VERIFICATION_PENDING`` to ``VERIFICATION`` for exactly the
    reason the two renames above give, and unaliases it: the suffix is reserved
    for unimplemented stages, and Phase 11's stage is implemented.  Do not add a
    ``VERIFICATION_PENDING`` alias back.

    The four states Phase 11 adds between the gate and the plan are the load-
    bearing part of this enum:

    ``FIX_APPROVED``
        A human approved a specific proposal, by fingerprint, and nothing has
        been written yet.  It exists so "approved" and "applied" are separate
        claims.  No stage is registered for it: the only way out is the
        orchestrator's ``approve_fix``, which is what makes the gate a gate.
    ``FIX_APPLYING``
        The approved change is being written, and the post-image is being
        confirmed.  The run occupies this state only while that happens.
    ``FIX_APPLIED``
        The repository holds the approved change, re-read and confirmed.  Only
        now may the run reach ``TEST_PLAN`` — the phase that outlines a
        regression test runs against a repository that already has the fix.
    ``VERIFIED`` / ``VERIFICATION_FAILED``
        A runner exited, and the outcome is recorded.  ``VERIFIED`` is the only
        state from which ``COMPLETED`` is reachable, and reaching it requires a
        real exit code rather than an absence of evidence.

    ``VERIFICATION_FAILED`` is terminal and distinct from ``FAILED`` on purpose.
    ``FAILED`` means the workflow broke: a stage raised, a repository could not
    be read, the proposal did not match its approval.  ``VERIFICATION_FAILED``
    means the workflow worked perfectly and the answer was no: the tests ran and
    did not pass.  Reporting those two with one word would make a failed
    regression suite indistinguishable from a broken tool.
    """

    CREATED = "CREATED"
    REPOSITORY_CONTEXT = "REPOSITORY_CONTEXT"
    ARCHITECTURE_CONTEXT = "ARCHITECTURE_CONTEXT"
    INVESTIGATION_PENDING = "INVESTIGATION_PENDING"
    EVIDENCE_PENDING = "EVIDENCE_PENDING"
    ROOT_CAUSE_ANALYSIS = "ROOT_CAUSE_ANALYSIS"
    ROOT_CAUSE_READY = "ROOT_CAUSE_READY"
    FIX_PROPOSAL = "FIX_PROPOSAL"
    FIX_PROPOSAL_READY = "FIX_PROPOSAL_READY"
    FIX_APPROVED = "FIX_APPROVED"
    FIX_APPLYING = "FIX_APPLYING"
    FIX_APPLIED = "FIX_APPLIED"
    TEST_PLAN = "TEST_PLAN"
    VERIFICATION = "VERIFICATION"
    VERIFIED = "VERIFIED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


class RunStatus(str, Enum):
    """Coarse run status, independent of the workflow state."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ── Events ────────────────────────────────────────────────────────────────────


class WorkflowEventType(str, Enum):
    """Types of events emitted by the orchestrator.

    ``STAGE_PENDING`` and ``RUN_CANCELLED`` extend the Phase 6 minimum set so
    that a placeholder stage and a cancellation are reported honestly rather
    than being reported as completions.

    Phase 7 adds the five ``INVESTIGATION_*`` / ``INVESTIGATOR_*`` events.  They
    exist because a stage that runs three agents in parallel needs to be
    *watchable*: a client has to see each investigator start and finish, not just
    a single opaque stage result.

    Phase 8 adds ``EVIDENCE_SYNTHESIS_*`` for the synthesis substep and
    ``ROOT_CAUSE_ANALYSIS_*`` for the analysis as a whole.  The failure event
    exists so that a synthesis that breaks is reported as a failure rather than
    silently leaving the run parked.

    Phase 9 adds ``FIX_PROPOSAL_*``.  A proposal that is merely *insufficient*
    is not a failure: it is reported through ``FIX_PROPOSAL_COMPLETED`` with a
    status that says so, because "we looked and could not responsibly propose a
    change" is a real answer and must not be rendered as a crash.

    Phase 10 adds ``TEST_PLAN_*``, following the Phase 9 convention: a plan that
    declines is a completion carrying an honest status, not a failure.  There is
    deliberately no ``TEST_GENERATED`` or ``TESTS_PASSED`` event, because Phase
    10 writes no test and runs none; adding such an event would create a signal a
    client could subscribe to and infer a result that does not exist.

    Phase 11 adds the write path and the executed-result path, and it is careful
    about the same trap.  ``FIX_APPROVED`` and ``FIX_APPLIED`` are two events
    because "a human said yes" and "the file changed" are two facts with a
    window between them in which a crash, a stale proposal or a read-only
    filesystem can separate them.  ``VERIFICATION_COMPLETED`` is a completion
    event whose payload carries ``status``; the *status* is a pass or a failure,
    and there is no ``TESTS_PASSED`` event, because an event type a client can
    filter on would let "passed" be read as a property of the run rather than of
    one execution.
    """

    RUN_CREATED = "RUN_CREATED"
    STATE_CHANGED = "STATE_CHANGED"
    STAGE_STARTED = "STAGE_STARTED"
    STAGE_COMPLETED = "STAGE_COMPLETED"
    STAGE_PENDING = "STAGE_PENDING"
    STAGE_FAILED = "STAGE_FAILED"
    RUN_COMPLETED = "RUN_COMPLETED"
    RUN_FAILED = "RUN_FAILED"
    RUN_CANCELLED = "RUN_CANCELLED"

    # ── Phase 7: parallel investigation ──────────────────────────────────
    #: The investigation stage began; emitted once per run.
    INVESTIGATION_STARTED = "INVESTIGATION_STARTED"
    #: One investigator began work.  Emitted for all three before any finishes,
    #: in canonical order, so the log shows genuine parallelism rather than a
    #: serialised approximation of it.
    INVESTIGATOR_STARTED = "INVESTIGATOR_STARTED"
    #: One investigator finished successfully, with its evidence count.
    INVESTIGATOR_COMPLETED = "INVESTIGATOR_COMPLETED"
    #: One investigator failed.  Its siblings' evidence is unaffected.
    INVESTIGATOR_FAILED = "INVESTIGATOR_FAILED"
    #: The investigation finished, with the aggregate status and error list.
    INVESTIGATION_COMPLETED = "INVESTIGATION_COMPLETED"

    # ── Phase 8: evidence synthesis and root-cause analysis ───────────────
    #: Deduplication, indexing and conflict detection over the Phase 7 evidence.
    EVIDENCE_SYNTHESIS_STARTED = "EVIDENCE_SYNTHESIS_STARTED"
    #: Synthesis finished, with the finding/evidence counts it consumed.
    EVIDENCE_SYNTHESIS_COMPLETED = "EVIDENCE_SYNTHESIS_COMPLETED"
    #: The root-cause analysis stage began; emitted once per run.
    ROOT_CAUSE_ANALYSIS_STARTED = "ROOT_CAUSE_ANALYSIS_STARTED"
    #: The analysis produced a verdict, with its status, confidence and level.
    ROOT_CAUSE_ANALYSIS_COMPLETED = "ROOT_CAUSE_ANALYSIS_COMPLETED"
    #: Synthesis or analysis could not be completed.  The run fails.
    ROOT_CAUSE_ANALYSIS_FAILED = "ROOT_CAUSE_ANALYSIS_FAILED"

    # ── Phase 9: fix proposal ─────────────────────────────────────────────
    #: The proposal stage began.  Emitted once per run.
    FIX_PROPOSAL_STARTED = "FIX_PROPOSAL_STARTED"
    #: A proposal was drafted, with its status, risk level and change count.
    #: ``DIFF_GENERATED`` is *not* a separate event: the diff is the proposal,
    #: and a distinct "diff generated" signal would let a client infer that a
    #: diff was verified or applied, which is exactly the confusion this
    #: project's evidence rules exist to prevent.
    FIX_PROPOSAL_COMPLETED = "FIX_PROPOSAL_COMPLETED"
    #: The proposal could not be produced at all (an unexpected error, not an
    #: honest "insufficient evidence" verdict).  The run fails.
    FIX_PROPOSAL_FAILED = "FIX_PROPOSAL_FAILED"

    # ── Phase 10: test plan ──────────────────────────────────────────────
    #: The planning stage began.  Emitted once per run.
    TEST_PLAN_STARTED = "TEST_PLAN_STARTED"
    #: A plan was recorded, with its status, gap count and scenario count.
    #: ``tests_executed`` is in the payload and is always False: Phase 10
    #: outlines tests in prose and runs nothing.
    TEST_PLAN_COMPLETED = "TEST_PLAN_COMPLETED"
    #: The plan could not be produced at all (an unexpected error, not an honest
    #: "insufficient evidence" verdict).  The run fails.
    TEST_PLAN_FAILED = "TEST_PLAN_FAILED"

    # ── Phase 11: approval, application and verification ────────────────
    #: A human approved a specific proposal, by fingerprint.  Nothing has been
    #: written yet — this event asserts intent, not effect.
    FIX_APPROVED = "FIX_APPROVED"
    #: The run reached the gate and is waiting for a human.  This is a *fact about
    #: the run*, not a missing implementation, and it gets its own event rather
    #: than a ``STAGE_PENDING`` wiring gap: the gate means "a diff is ready for
    #: your review", and reporting that in a vocabulary which says the software is
    #: unfinished is how a working system gets demoed as a broken one.
    FIX_APPROVAL_REQUIRED = "FIX_APPROVAL_REQUIRED"
    #: The approved change is about to be written.  ``FIX_APPROVED`` and
    #: ``FIX_APPLIED`` are separate events precisely so a client can tell an
    #: interrupted run from a completed one.
    FIX_APPLICATION_STARTED = "FIX_APPLICATION_STARTED"
    #: The approved change was written and the post-image re-read and confirmed.
    FIX_APPLIED = "FIX_APPLIED"
    #: The application was refused or failed.  ``status`` in the payload says
    #: which, because "stale" and "unsafe" have different remedies.
    FIX_APPLICATION_FAILED = "FIX_APPLICATION_FAILED"
    #: The controlled runner is being started.
    VERIFICATION_STARTED = "VERIFICATION_STARTED"
    #: The runner finished.  ``status`` in the payload is the verdict; a
    #: ``FAILED`` verdict still completes this stage, because a regression suite
    #: that does not pass is an answer rather than a crash.
    VERIFICATION_COMPLETED = "VERIFICATION_COMPLETED"
    #: Verification could not produce a verdict at all (blocked, or the runner
    #: raised).  Distinguished from ``VERIFICATION_COMPLETED`` so "still owed" and
    #: "answered no" cannot be read the same way.
    VERIFICATION_FAILED = "VERIFICATION_FAILED"


class WorkflowEvent(BaseModel):
    """A single append-only workflow event.

    ``sequence`` is a per-run, gap-free counter starting at 1.  It defines the
    event order and is used as the SSE resume cursor.
    """

    event_id: str
    run_id: str
    sequence: int
    event_type: WorkflowEventType
    state: WorkflowState
    status: RunStatus
    message: str
    timestamp: datetime = Field(default_factory=utc_now)
    payload: dict[str, Any] = Field(default_factory=dict)


# ── Stages ────────────────────────────────────────────────────────────────────


class StageOutcome(str, Enum):
    """What a stage handler reports back to the orchestrator.

    COMPLETED
        The stage really ran and produced results.  The orchestrator may move
        to ``next_state`` if the state machine allows it.
    PENDING
        The stage is a known-but-unimplemented placeholder.  The orchestrator
        does **not** advance.  In particular a placeholder must never cause a
        run to reach ``COMPLETED``.
    FAILED
        The stage hit an unrecoverable error.  The orchestrator moves the run
        to ``FAILED``.
    """

    COMPLETED = "COMPLETED"
    PENDING = "PENDING"
    FAILED = "FAILED"


class StageResult(BaseModel):
    """The typed output of a single stage execution.

    ``next_state`` is the state the run should occupy once the stage succeeds.
    It must equal the state the handler is registered against; the
    orchestrator cross-checks it and will not advance on a mismatch.

    ``failure_state`` exists for the one case where "failed" and "where it went"
    are different questions.  A stage whose whole job is to produce a verdict —
    Phase 11's verification stage — has two kinds of bad outcome: the stage broke
    (the run is ``FAILED``), and the stage worked and the answer was no (the run
    is ``VERIFICATION_FAILED``).  Reporting both as ``FAILED`` would make a
    regression suite that does not pass indistinguishable from a crashed tool.

    The field is constrained to ``None`` or a failure state, and the orchestrator
    validates that the target is actually reachable from the current state, so a
    stage still cannot steer the workflow through its failure path.  A stage
    that omits it fails exactly as it did before Phase 11.
    """

    outcome: StageOutcome
    message: str
    next_state: WorkflowState | None = None
    error: str | None = None
    #: Where the run goes when ``outcome`` is ``FAILED``.  ``None`` means
    #: ``WorkflowState.FAILED``, which is every pre-Phase-11 stage's behaviour.
    failure_state: WorkflowState | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _failure_state_must_be_a_failure_state(self) -> "StageResult":
        """A stage may only ever ask to be failed — never advanced.

        Without this, a stage could return ``outcome=FAILED`` with
        ``failure_state=VERIFIED`` and the orchestrator's transition check would
        happily move the run there.  The field exists to *choose between failure
        states*, not to smuggle a success path through the failure branch, so the
        constraint is enforced here rather than trusted at the call site.

        The two members are exactly the two
        :meth:`app.services.state_machine.WorkflowStateMachine.is_failure_state`
        recognises: ``FAILED`` is a broken workflow, ``VERIFICATION_FAILED`` is a
        working one that produced a negative answer.  ``CANCELLED`` is excluded
        because cancellation is a server-side decision about a run, not something
        a stage reports about itself — and a stage that could cancel its own run
        would be able to end a workflow by failing it.
        """
        if self.failure_state is not None and self.failure_state not in (
            WorkflowState.FAILED,
            WorkflowState.VERIFICATION_FAILED,
        ):
            raise ValueError(
                f"failure_state must be FAILED or VERIFICATION_FAILED, not "
                f"{self.failure_state.value}; a stage cannot advance the workflow "
                f"through its failure path, and it cannot cancel its own run"
            )
        return self

    def resolved_failure_state(self) -> WorkflowState:
        """The state a ``FAILED`` result should move the run to."""
        return self.failure_state or WorkflowState.FAILED

    @classmethod
    def pending(cls, message: str, **data: Any) -> "StageResult":
        """Honest placeholder result: no state change, no evidence."""
        return cls(
            outcome=StageOutcome.PENDING,
            message=message,
            next_state=None,
            data={"implemented": False, **data},
            evidence=[],
        )

    @classmethod
    def completed(
        cls,
        message: str,
        next_state: WorkflowState,
        evidence: list[Evidence] | None = None,
        **data: Any,
    ) -> "StageResult":
        """Successful stage result that requests a state transition."""
        return cls(
            outcome=StageOutcome.COMPLETED,
            message=message,
            next_state=next_state,
            data=data,
            evidence=evidence or [],
        )

    @classmethod
    def failed(
        cls,
        message: str,
        error: str,
        failure_state: WorkflowState | None = None,
        evidence: list[Evidence] | None = None,
        **data: Any,
    ) -> "StageResult":
        """Unrecoverable stage failure.

        ``failure_state`` defaults to ``None``, which the orchestrator resolves to
        ``WorkflowState.FAILED`` — so every stage written before Phase 11 behaves
        identically without being touched.

        ``data`` and ``evidence`` exist because a Phase 11 stage can fail *and*
        have something to report.  A blocked verification produced a real
        ``BLOCKED`` result — a reason, a status, a set of limitations — and
        dropping it on the floor because the stage returned a failure would make
        "we could not run the tests" indistinguishable from "we did not try".
        The evidence list is the reason a blocked outcome must still be able to
        carry it: ``_no_verified_result_without_a_pass`` asserts that a
        ``VERIFIED_RESULT`` can only arrive on a real pass, and that assertion is
        much easier to hold when a failing stage can record what it knows.
        """
        return cls(
            outcome=StageOutcome.FAILED,
            message=message,
            next_state=None,
            error=error,
            failure_state=failure_state,
            data=data,
            evidence=evidence or [],
        )


# ── Run ───────────────────────────────────────────────────────────────────────


class MentorRun(BaseModel):
    """A single mentor workflow run and its inspectable state.

    ``current_state``, ``status`` and ``error`` are written by the orchestrator
    only.  Clients can read them but can never set them.
    """

    run_id: str
    repository_path: str
    issue: str
    current_state: WorkflowState = WorkflowState.CREATED
    status: RunStatus = RunStatus.PENDING
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    #: Append-only, server-assigned.  The full history lives in the event store.
    evidence: list[Evidence] = Field(default_factory=list)
    #: Stage-produced, deterministic summary data (counts, names, notes).
    context: dict[str, Any] = Field(default_factory=dict)
