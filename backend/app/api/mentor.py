"""
Mentor workflow REST API — Phases 6–11.

The whole client-facing surface of the mentor, in the order a client meets it::

    POST /api/mentor/runs                          create a run and drive it
    GET  /api/mentor/runs/{run_id}                 the run, its evidence and context
    GET  /api/mentor/runs/{run_id}/investigation   Phase 7: investigators + evidence
    GET  /api/mentor/runs/{run_id}/root-cause      Phase 8: the static analysis
    GET  /api/mentor/runs/{run_id}/fix-proposal    Phase 9: the diff awaiting review
    POST /api/mentor/runs/{run_id}/approve-fix     Phase 11: the human approval gate
    GET  /api/mentor/runs/{run_id}/fix-application Phase 11: what was written
    GET  /api/mentor/runs/{run_id}/test-plan       Phase 10: the outlined scenarios
    GET  /api/mentor/runs/{run_id}/verification    Phase 11: what was executed
    POST /api/mentor/runs/{run_id}/cancel          operator cancellation

``GET /api/mentor/runs/{run_id}/events`` — the SSE stream — is **not** here.  It
belongs to :mod:`app.api.stream`, and this router is registered first, so a second
route on the same path here would shadow the stream with a JSON response.  Event
history is reachable from ``GET /api/mentor/runs/{run_id}`` via ``event_count`` and
``last_event_sequence``.

Three rules this module exists to enforce
-----------------------------------------
1. **The server owns the workflow.**  ``current_state``, ``status`` and ``error``
   are written by the orchestrator and are readable here but not settable: the only
   request bodies in this module carry a repository reference, an issue text, and —
   from Phase 11 — a reviewer's name, an optional note and an optional
   compare-and-swap fingerprint.  ``extra="forbid"`` on every request model turns an
   attempt to smuggle a state, a diff, a path or a command into a 422 instead of a
   silently ignored field.
2. **The gate is the only door to a write.**  ``POST .../approve-fix`` records the
   approval and then delegates the whole write-then-verify sequence to
   ``MentorOrchestrator.approve_and_continue``.  This module opens no file, runs no
   command and touches no repository: the approver that owns the preconditions lives
   in ``app.services.fix_applier``, behind the orchestrator.
3. **A read endpoint re-derives nothing.**  Every ``GET`` below reconstructs its
   answer from ``run.context`` through the single loader its phase provides
   (``load_investigation``, ``load_root_cause``, ``load_fix_proposal``,
   ``load_test_plan``, ``load_fix_application``, ``load_verification``).  A second
   parser of stored context would be free to disagree with the one that wrote it.

Status codes are the same everywhere, and the distinctions are load-bearing:

* ``400`` — the run id is not a server-issued UUID4 string.  Rejected before it
  reaches persistence.
* ``404`` — the run id is well formed but unknown.  The detail names the run.
* ``409`` — the run exists, but the stage this endpoint reports on has not run.
  "Not investigated yet" and "investigated and found nothing" are different answers
  and must not both render as an empty 200.
* ``422`` — the request body is invalid, oversized, or carries a field the endpoint
  has no business accepting.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.domain import Evidence
from app.models.fix_application import FixApplicationResult
from app.models.fix_approval import (
    MAX_APPROVAL_COMMENT_LENGTH,
    MAX_APPROVER_LENGTH,
    FixApproval,
    is_well_formed_fingerprint,
)
from app.models.fix_proposal import AnchorLocation, FixProposal
from app.models.investigation import InvestigatorReport
from app.models.root_cause import RootCauseAnalysis
from app.models.test_plan import CoverageGap, TestPlan, TestScenario
from app.models.verification import VerificationResult
from app.models.workflow import (
    MAX_ISSUE_LENGTH,
    MAX_REPOSITORY_PATH_LENGTH,
    MentorRun,
    WorkflowEvent,
    is_valid_run_id,
)
from app.services import fix_application_stage, verification_stage
from app.services.fix_approval import (
    FixApprovalRejected,
    load_approval,
    proposal_fingerprint,
)
from app.services.fix_proposal_stage import (
    FIX_PROPOSAL_CREATED_AT_KEY,
    load_fix_proposal,
)
from app.services.investigation_stage import (
    INVESTIGATION_SUMMARY_KEY,
    load_investigation,
)
from app.services.orchestrator import (
    MentorOrchestrator,
    RunNotFoundError,
    RunValidationError,
    get_default_orchestrator,
)
from app.services.root_cause_stage import (
    ROOT_CAUSE_SYNTHESIZED_AT_KEY,
    load_root_cause,
)
from app.services.state_machine import InvalidTransitionError, WorkflowStateMachine
from app.services.test_plan_stage import (
    TEST_PLAN_CREATED_AT_KEY,
    load_test_plan,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mentor", tags=["mentor"])


def orchestrator_dependency() -> MentorOrchestrator:
    """FastAPI dependency returning the shared orchestrator instance."""
    return get_default_orchestrator()


# ── Request models ────────────────────────────────────────────────────────────
#
# Every field here is either a repository reference, a piece of prose, or the name
# of the human who reviewed a diff.  None of them can express a state transition, a
# file to write, a diff to apply or a command to run, and ``extra="forbid"`` means
# adding one later is a deliberate, visible act rather than a convenience.


class CreateRunRequest(BaseModel):
    """Body for ``POST /api/mentor/runs``.

    The only two fields, deliberately.  A client supplies *what* to look at and
    *what was observed*; everything else — the run id, the state, the status, the
    evidence, the context, the timestamps — is server-owned and rejected with a 422
    if a client tries to set it.
    """

    model_config = ConfigDict(extra="forbid")

    repository_path: str = Field(
        ...,
        min_length=1,
        max_length=MAX_REPOSITORY_PATH_LENGTH,
        description="Local path or repository reference to investigate.",
    )
    issue: str = Field(
        ...,
        min_length=1,
        max_length=MAX_ISSUE_LENGTH,
        description="The developer's natural-language bug report or question.",
    )

    @field_validator("repository_path", "issue")
    @classmethod
    def _must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank or whitespace-only")
        if "\x00" in value:
            raise ValueError("must not contain NUL bytes")
        return value


class ApproveFixRequest(BaseModel):
    """Body for ``POST /api/mentor/runs/{run_id}/approve-fix``.

    The gate's entire vocabulary: who reviewed it, an optional note, and optionally
    the fingerprint the client believes it is approving.

    * There is **no** ``diff``, ``file_path``, ``line``, ``before``, ``after``,
      ``command`` or ``content`` field, and ``extra="forbid"`` turns a request that
      carries one into a 422 rather than a silently dropped key.  The change that
      will be written is the one already stored on the run.
    * ``approved`` is ``Literal[True]`` with a default, so the type cannot express a
      refusal.  A refusal is an HTTP 409 that records nothing; a stored
      ``approved=False`` would be a permanent veto that later phases would have to
      learn to honour, which is how a gate acquires a bypass.
    * ``expected_fingerprint`` is the compare-and-swap guard: a client that rendered
      a diff and then saw it change underneath it can prove *which* diff it approved.
      It is validated for shape here (422 for nonsense) and compared against the
      stored proposal by the orchestrator (409 for a well-formed mismatch).
    """

    model_config = ConfigDict(extra="forbid")

    approved_by: str = Field(
        ...,
        min_length=1,
        max_length=MAX_APPROVER_LENGTH,
        description=(
            "Audit label for the reviewer. Not an authenticated identity — the "
            "project has no auth layer, and the limitation is carried in the "
            "application result's limitations."
        ),
    )
    comment: str | None = Field(
        default=None,
        max_length=MAX_APPROVAL_COMMENT_LENGTH,
        description="Free-text review note. Stored and echoed; never interpreted.",
    )
    expected_fingerprint: str | None = Field(
        default=None,
        description=(
            "The proposal fingerprint the client displayed. Optional; supplied by "
            "a UI that showed the diff before asking for approval."
        ),
    )
    approved: Literal[True] = True

    @field_validator("approved_by")
    @classmethod
    def _reviewer_must_be_usable(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("approved_by must not be blank")
        if "\x00" in value:
            raise ValueError("approved_by must not contain NUL bytes")
        return value

    @field_validator("comment")
    @classmethod
    def _comment_must_be_usable(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:
            raise ValueError("comment must not contain NUL bytes")
        return value

    @field_validator("expected_fingerprint")
    @classmethod
    def _fingerprint_must_be_well_formed(cls, value: str | None) -> str | None:
        if value is not None and not is_well_formed_fingerprint(value):
            raise ValueError(
                "expected_fingerprint must be 'sha256:' followed by 64 lowercase "
                "hexadecimal characters"
            )
        return value


# ── Response models ───────────────────────────────────────────────────────────
#
# A response model is a domain model plus exactly the transport fields its phase
# needs — the state the run was read at, the wall clock the domain models keep out,
# and the honesty booleans that have to be visible rather than merely true.  Where
# a field could be softened (``applied``, ``tests_executed``, ``test_source_written``)
# it is hard-wired from a literal in the view function below, not read off the model.


class MentorRunResponse(BaseModel):
    """A run, as the client is allowed to see it.

    ``event_count`` and ``last_event_sequence`` are the durable-history pointer:
    they let a client that missed the SSE stream find out how much it missed.  They
    are equal whenever the sequence is gap-free, which is what the run store
    guarantees.
    """

    run_id: str
    repository_path: str
    issue: str
    current_state: str
    status: str
    error: str | None = None
    created_at: str
    updated_at: str
    is_terminal: bool = False
    event_count: int = 0
    last_event_sequence: int = 0
    evidence: list[Evidence] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)


class InvestigationResponse(BaseModel):
    """The Phase 7 investigation.

    Two fields are load-bearing.  ``tests_executed`` is always ``False`` because
    Phase 7 executes nothing, and there is no ``root_cause`` field at all: the
    Phase 7 view points at the Phase 8 conclusion through the derived
    ``root_cause_claimed`` flag and never inlines root-cause text of its own, so the
    two endpoints cannot drift into disagreeing about what was concluded.
    """

    run_id: str
    run_state: str
    status: str
    investigators: list[InvestigatorReport] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    evidence_counts: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    #: Reported from the stored summary rather than from the rebuilt model, so two
    #: reads of the same run are byte-identical.
    started_at: str | None = None
    completed_at: str | None = None
    tests_executed: bool = False
    root_cause_claimed: bool = False


class RootCauseResponse(RootCauseAnalysis):
    """The Phase 8 analysis plus the state it was read at and the claim flag.

    ``synthesized_at`` is the wall clock, which the analysis model deliberately does
    not carry so that two runs over identical evidence produce byte-identical
    analyses.  ``root_cause_claimed`` is *derived* from the status and the root
    cause text — it is never asserted independently, so a client cannot read it as a
    second opinion about whether a defect was found.
    """

    run_state: str
    root_cause_claimed: bool
    synthesized_at: str | None = None


class FixProposalResponse(FixProposal):
    """The Phase 9 proposal as a reviewer sees it.

    ``applied`` and ``tests_executed`` are additional to the domain model and are
    both hard-wired ``False`` in the view function: a client that reads a diff must
    not be able to mistake it for a change that was made or a change that passed
    something.  ``created_at`` likewise comes from ``run.context``, not the model.
    """

    run_state: str
    ready_for_review: bool = False
    #: Always ``False``.  Hard-wired from a literal in the view function.
    applied: bool = False
    added_lines: int = 0
    removed_lines: int = 0
    #: The content fingerprint a client echoes back as ``expected_fingerprint``.
    #: Computed with the *same* function the gate and the applier use, so the
    #: value a reviewer is shown and the value the orchestrator compares against
    #: cannot drift apart.  ``None`` for a proposal that is not ready for review,
    #: which mirrors the gate's own ``FIX_APPROVAL_REQUIRED`` payload: there is
    #: nothing to approve, so there is no fingerprint to quote.
    proposal_fingerprint: str | None = None
    created_at: str | None = None


class TestPlanResponse(BaseModel):
    """The Phase 10 plan.

    Declared rather than inherited, for one reason: ``execution_status`` is a plain
    ``str`` here.  Inheriting :class:`~app.models.test_plan.TestPlan` would inherit
    its enum-typed field and its invariant that the value is exactly
    ``TestExecutionStatus.NOT_RUN``; widening the type to ``str`` on the way out
    keeps the response from ever being widened into something a UI could treat as a
    pass.
    """

    run_id: str
    issue: str
    run_state: str
    status: str
    rationale: str
    #: Reused from :class:`~app.models.fix_proposal.AnchorLocation` rather than
    #: re-declared, so a plan can never widen the transport shape into something
    #: that carries quoted source.
    anchor: AnchorLocation | None = None
    root_cause_kind: str | None = None
    root_cause_confidence: float = 0.0
    fix_proposal_status: str | None = None
    coverage_gaps: list[CoverageGap] = Field(default_factory=list)
    scenarios: list[TestScenario] = Field(default_factory=list)
    #: Always ``"NOT_RUN"``: Phase 10 outlines scenarios and writes no test source.
    execution_status: str
    ready_for_review: bool = False
    #: Hard-wired from literals in the view function, never from the model.
    tests_executed: bool = False
    test_source_written: bool = False
    assumptions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: str | None = None

    #: Not a pytest class — see the same convention on the domain enums.
    __test__ = False


class FixApplicationResponse(FixApplicationResult):
    """The Phase 11 application record.

    ``repository_modified`` is the one field the domain model does not carry, because
    the property lives on the status enum.  It is derived, not stored: only
    ``APPLIED`` leaves changed bytes behind, ``ALREADY_APPLIED`` wrote nothing and
    ``FAILED_ROLLED_BACK`` deliberately undid the write, and a record that reported
    a rollback as "repository modified" would be the most alarming and least true
    thing this endpoint could say.
    """

    repository_modified: bool = False


class ApproveFixResponse(BaseModel):
    """The whole Phase 11 story in one response.

    A client that never opened an SSE stream still gets the full sequence: the run
    (which reaches ``COMPLETED`` only on a real exit code 0), the approval, the
    application record, the verification result, and the workflow events the
    approval produced — in order, so the gap between "a human said yes" and "the
    file changed" stays visible.
    """

    run: MentorRunResponse
    approval: FixApproval
    application: FixApplicationResponse
    verification: VerificationResult
    events: list[WorkflowEvent] = Field(default_factory=list)


# ── Lookup helpers ────────────────────────────────────────────────────────────


def _reject_malformed_run_id(run_id: str) -> None:
    """Map a client-supplied run id to a 400 before it reaches persistence.

    Run ids are server-issued UUID4 strings in lowercase hex.  Anything else —
    ``../etc/passwd``, ``1' OR '1'='1``, an uppercased UUID, a path fragment — never
    becomes a query parameter.
    """
    if not is_valid_run_id(run_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "run_id must be a server-issued UUID4 identifier and contain only "
                "lowercase hexadecimal characters"
            ),
        )


def _load_run(orchestrator: MentorOrchestrator, run_id: str) -> MentorRun:
    """Return the run for ``run_id``, mapping the two lookup failures to statuses."""
    _reject_malformed_run_id(run_id)
    try:
        return orchestrator.get_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc


# ── View helpers ──────────────────────────────────────────────────────────────
#
# Each of these turns stored state into the response model for one phase.  They are
# read-only: nothing here writes, transitions, approves or advances.


def _evidence_counts(evidence: list[Evidence]) -> dict[str, int]:
    """Count the evidence items by type.  Every count is non-zero by construction."""
    counts: dict[str, int] = {}
    for item in evidence:
        key = item.evidence_type.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _run_view(orchestrator: MentorOrchestrator, run: MentorRun) -> MentorRunResponse:
    """Project a run for the client, including its event-log position."""
    return MentorRunResponse(
        run_id=run.run_id,
        repository_path=run.repository_path,
        issue=run.issue,
        current_state=run.current_state.value,
        status=run.status.value,
        error=run.error,
        created_at=run.created_at.isoformat(),
        updated_at=run.updated_at.isoformat(),
        is_terminal=WorkflowStateMachine.is_terminal(run.current_state),
        event_count=orchestrator.event_count(run.run_id),
        last_event_sequence=orchestrator.latest_event_sequence(run.run_id),
        evidence=list(run.evidence),
        context=dict(run.context),
    )


def _investigation_view(
    run: MentorRun, investigation: Any
) -> InvestigationResponse:
    """Project the Phase 7 investigation, claiming no root cause of its own."""
    summary = run.context.get(INVESTIGATION_SUMMARY_KEY)
    summary = summary if isinstance(summary, dict) else {}
    analysis = load_root_cause(run)

    return InvestigationResponse(
        run_id=run.run_id,
        run_state=run.current_state.value,
        status=investigation.status.value,
        investigators=list(investigation.investigators),
        evidence=list(investigation.evidence),
        evidence_counts=_evidence_counts(list(investigation.evidence)),
        errors=list(investigation.errors),
        started_at=summary.get("started_at"),
        completed_at=summary.get("completed_at"),
        # Phase 7 executed nothing.  Hard-wired, not read off the model.
        tests_executed=False,
        # Derived from the Phase 8 verdict, never asserted independently.
        root_cause_claimed=bool(analysis is not None and analysis.is_determined()),
    )


def _root_cause_view(run: MentorRun, analysis: RootCauseAnalysis) -> RootCauseResponse:
    """Project the Phase 8 analysis with the run state and the claim flag."""
    data = analysis.model_dump(mode="json")
    data["run_state"] = run.current_state.value
    data["root_cause_claimed"] = analysis.is_determined()
    data["synthesized_at"] = run.context.get(ROOT_CAUSE_SYNTHESIZED_AT_KEY)
    return RootCauseResponse.model_validate(data)


def _fix_proposal_view(run: MentorRun, proposal: FixProposal) -> FixProposalResponse:
    """Project the Phase 9 proposal for a reviewer.

    ``applied`` and ``tests_executed`` are literals, not lookups: Phase 9 applied
    nothing and ran nothing, and a future refactor that reads them off the model
    instead would be the first step towards a proposal that claims to be verified.
    """
    reviewable = proposal.is_ready_for_review()

    return FixProposalResponse(
        **proposal.model_dump(mode="json"),
        run_state=run.current_state.value,
        ready_for_review=reviewable,
        applied=False,
        added_lines=proposal.added_line_count(),
        removed_lines=proposal.removed_line_count(),
        proposal_fingerprint=proposal_fingerprint(proposal) if reviewable else None,
        created_at=run.context.get(FIX_PROPOSAL_CREATED_AT_KEY),
    )


def _test_plan_view(run: MentorRun, plan: TestPlan) -> TestPlanResponse:
    """Project the Phase 10 plan.  A plan is not a written test and not a result."""
    return TestPlanResponse(
        run_id=plan.run_id,
        issue=plan.issue,
        run_state=run.current_state.value,
        status=plan.status.value,
        rationale=plan.rationale,
        anchor=plan.anchor,
        root_cause_kind=(
            plan.root_cause_kind.value if plan.root_cause_kind is not None else None
        ),
        root_cause_confidence=plan.root_cause_confidence,
        fix_proposal_status=(
            plan.fix_proposal_status.value
            if plan.fix_proposal_status is not None
            else None
        ),
        coverage_gaps=list(plan.coverage_gaps),
        scenarios=list(plan.scenarios),
        execution_status=plan.execution_status.value,
        ready_for_review=plan.is_ready_for_review(),
        tests_executed=False,
        test_source_written=False,
        assumptions=list(plan.assumptions),
        limitations=list(plan.limitations),
        created_at=run.context.get(TEST_PLAN_CREATED_AT_KEY),
    )


def _fix_application_view(application: FixApplicationResult) -> FixApplicationResponse:
    """Project the Phase 11 application record with the derived modification flag."""
    return FixApplicationResponse(
        **application.model_dump(mode="json"),
        repository_modified=application.status.changed_repository,
    )


# ── Run endpoints ─────────────────────────────────────────────────────────────


@router.post(
    "/runs",
    response_model=MentorRunResponse,
    status_code=201,
    summary="Create a mentor run and drive it as far as it can go",
    responses={
        201: {"description": "Run created and walked to its current boundary."},
        422: {"description": "Invalid, blank or oversized body."},
    },
)
async def create_run(
    body: CreateRunRequest,
    orchestrator: MentorOrchestrator = Depends(orchestrator_dependency),
) -> MentorRunResponse:
    """Create a run, then walk it to the boundary it stops at.

    The walk is synchronous on purpose.  ``run_until_stalled()`` is deterministic —
    the same repository and issue always take the same path and stop at the same
    state — and a client that receives the run already at its boundary does not have
    to poll to find out whether the system worked.  Today that boundary is
    ``FIX_PROPOSAL_READY``, the human approval gate.

    A repository that cannot be read is not a bad request: the run is created, fails
    in its first stage and comes back as a 201 with ``status=FAILED`` and a reason.
    A run that reports its own failure is more useful than a 400 the client has to
    interpret.
    """
    try:
        run = orchestrator.create_run(
            repository_path=body.repository_path,
            issue=body.issue,
        )
    except RunValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.reason) from exc

    result = orchestrator.run_until_stalled(run.run_id)
    logger.info(
        "Run %s created and walked to %s (stalled=%s)",
        run.run_id,
        result.run.current_state.value,
        result.stalled,
    )
    return _run_view(orchestrator, result.run)


@router.get(
    "/runs/{run_id}",
    response_model=MentorRunResponse,
    summary="Get a mentor run with its evidence, context and event position",
    responses={
        200: {"description": "The run."},
        400: {"description": "Malformed run ID."},
        404: {"description": "Unknown run."},
    },
)
async def get_run(
    run_id: str,
    orchestrator: MentorOrchestrator = Depends(orchestrator_dependency),
) -> MentorRunResponse:
    """Return the run, read-only.

    Repeated calls are byte-identical: the run's timestamps, evidence and context
    are all stored, and this endpoint writes nothing.
    """
    run = _load_run(orchestrator, run_id)
    return _run_view(orchestrator, run)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=MentorRunResponse,
    summary="Cancel a non-terminal run",
    responses={
        200: {"description": "Run cancelled."},
        400: {"description": "Malformed run ID."},
        404: {"description": "Unknown run."},
        409: {"description": "The run is already terminal."},
    },
)
async def cancel_run(
    run_id: str,
    orchestrator: MentorOrchestrator = Depends(orchestrator_dependency),
) -> MentorRunResponse:
    """Cancel a run an operator has abandoned.

    Cancellation is a refusal the *run* records, and it outranks the approval gate:
    a cancelled run cannot be approved afterwards.  The transition still goes
    through the state machine — the endpoint does not set ``CANCELLED`` itself.
    """
    _load_run(orchestrator, run_id)

    try:
        run = orchestrator.cancel_run(run_id)
    except InvalidTransitionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    return _run_view(orchestrator, run)


# ── Phase 7: the investigation ────────────────────────────────────────────────


@router.get(
    "/runs/{run_id}/investigation",
    response_model=InvestigationResponse,
    summary="Get the Phase 7 investigation for a run",
    responses={
        200: {
            "description": (
                "The investigation. A run that has not been investigated returns "
                "PENDING with no investigators and no evidence, not a 404."
            )
        },
        400: {"description": "Malformed run ID."},
        404: {"description": "Unknown run."},
    },
)
async def get_investigation(
    run_id: str,
    orchestrator: MentorOrchestrator = Depends(orchestrator_dependency),
) -> InvestigationResponse:
    """Return the three investigators' reports and their typed evidence.

    "Not investigated yet" is a valid state rather than an error, so a run created
    but not walked answers 200 with ``status=PENDING`` and empty lists.
    """
    run = _load_run(orchestrator, run_id)
    investigation = load_investigation(run)
    return _investigation_view(run, investigation)


# ── Phase 8: the root-cause analysis ──────────────────────────────────────────


@router.get(
    "/runs/{run_id}/root-cause",
    response_model=RootCauseResponse,
    summary="Get the Phase 8 root-cause analysis for a run",
    responses={
        200: {
            "description": (
                "The static analysis. An honest negative is a 200 with no root "
                "cause, not an empty object."
            )
        },
        400: {"description": "Malformed run ID."},
        404: {"description": "Unknown run."},
        409: {"description": "The run has no analysis yet."},
    },
)
async def get_root_cause(
    run_id: str,
    orchestrator: MentorOrchestrator = Depends(orchestrator_dependency),
) -> RootCauseResponse:
    """Return the Phase 8 analysis.

    ``tests_executed`` and ``verified_result_count`` are always present, and always
    ``False`` / ``0``: Phase 8 reasons from evidence Phase 7 read out of the source,
    so a client cannot present a static reading as a reproduced defect.  ``root_cause``
    is ``None`` for every status that is not ``DETERMINED`` or ``PARTIAL``.
    """
    run = _load_run(orchestrator, run_id)
    analysis = load_root_cause(run)
    if analysis is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Run {run_id} is in {run.current_state.value} and has no root-cause "
                f"analysis: the ROOT_CAUSE_ANALYSIS stage has not produced one, so "
                f"there is nothing to report."
            ),
        )
    return _root_cause_view(run, analysis)


# ── Phase 9: the proposal awaiting review ─────────────────────────────────────


@router.get(
    "/runs/{run_id}/fix-proposal",
    response_model=FixProposalResponse,
    summary="Get the Phase 9 fix proposal for a run",
    responses={
        200: {
            "description": (
                "The proposal, reconstructed from stored context. A declined "
                "proposal is a 200 with a null diff."
            )
        },
        400: {"description": "Malformed run ID."},
        404: {"description": "Unknown run."},
        409: {"description": "No proposal has been recorded for this run."},
    },
)
async def get_fix_proposal(
    run_id: str,
    orchestrator: MentorOrchestrator = Depends(orchestrator_dependency),
) -> FixProposalResponse:
    """Return the diff waiting for a human, or an honest reason there is none.

    The diff is rebuilt from the run's stored proposal, never regenerated, so two
    reads cannot disagree and a client that approves what it saw here is approving
    exactly these bytes.  Reading it writes nothing: the repository keeps its bug.
    """
    run = _load_run(orchestrator, run_id)
    proposal = load_fix_proposal(run)
    if proposal is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Run {run_id} is in {run.current_state.value} and has no fix proposal "
                f"recorded: the FIX_PROPOSAL stage has not produced one, so there is "
                f"no diff to review."
            ),
        )
    return _fix_proposal_view(run, proposal)


# ── Phase 11: the human approval gate ─────────────────────────────────────────


@router.post(
    "/runs/{run_id}/approve-fix",
    response_model=ApproveFixResponse,
    summary="Approve the stored fix proposal, apply it and verify the result",
    responses={
        200: {"description": "Approval recorded, fix applied, verification recorded."},
        400: {"description": "Malformed run ID."},
        404: {"description": "Unknown run."},
        409: {
            "description": (
                "The approval was refused: the run is not at the gate, it carries no "
                "diff to review, the fingerprint is stale, or it was already approved."
            )
        },
        422: {"description": "Invalid body, or a body carrying a patch."},
    },
)
async def approve_fix(
    run_id: str,
    body: ApproveFixRequest,
    orchestrator: MentorOrchestrator = Depends(orchestrator_dependency),
) -> ApproveFixResponse:
    """Record a human's approval, then let the orchestrator drive Phases 10 and 11.

    This route is the only place in the system where a request becomes a write to
    somebody's source tree, and it delegates every step of it.  ``approve_and_continue``
    records the approval against the stored proposal's fingerprint, applies that
    proposal, re-reads the post-image, outlines the regression scenarios and runs
    the repository's own suite — then completes the run only if a real exit code
    justified it.  The applier re-checks the fingerprint immediately before writing,
    so a proposal edited between approval and application is caught rather than
    applied.

    Every refusal is a 409 carrying the reason a developer needs: wrong state, no
    diff, stale fingerprint, or a second approval of the same proposal.
    """
    _load_run(orchestrator, run_id)

    try:
        result = orchestrator.approve_and_continue(
            run_id=run_id,
            approved_by=body.approved_by,
            comment=body.comment or "",
            expected_fingerprint=body.expected_fingerprint,
        )
    except FixApprovalRejected as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except RunValidationError as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except RunNotFoundError as exc:  # pragma: no cover - checked immediately above
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc

    run = result.run
    approval = load_approval(run)
    application = fix_application_stage.load_fix_application(run)
    verification = verification_stage.load_verification(run)

    logger.info(
        "Run %s approved by %s: state=%s application=%s verification=%s",
        run_id,
        body.approved_by,
        run.current_state.value,
        application.status.value if application is not None else None,
        verification.status.value if verification is not None else None,
    )

    if approval is None or application is None or verification is None:
        # The gate and the applier own these three records; their absence after a
        # successful approval would be a bug in those stages, not an answer this
        # endpoint can honestly render as "nothing happened".
        raise HTTPException(
            status_code=500,
            detail=(
                "The approval was recorded but the run is missing its application or "
                "verification record. Inspect the run's workflow events."
            ),
        )

    return ApproveFixResponse(
        run=_run_view(orchestrator, run),
        approval=approval,
        application=_fix_application_view(application),
        verification=verification,
        events=list(result.events),
    )


# ── Phase 11 read endpoints ───────────────────────────────────────────────────
#
# Both are 409 until the approval has been recorded: a 200 with empty fields would
# let a client render "applied nothing" and "verified nothing" as though they were
# answers, when they are the absence of one.


@router.get(
    "/runs/{run_id}/fix-application",
    response_model=FixApplicationResponse,
    summary="Get the Phase 11 fix-application record for a run",
    responses={
        200: {"description": "The application record: paths, digests and counts."},
        400: {"description": "Malformed run ID."},
        404: {"description": "Unknown run."},
        409: {"description": "No fix has been applied to this run."},
    },
)
async def get_fix_application(
    run_id: str,
    orchestrator: MentorOrchestrator = Depends(orchestrator_dependency),
) -> FixApplicationResponse:
    """Return what was written to the repository, and how it was confirmed.

    Reconstructed from stored context, so it is byte-identical to what the approval
    reported and calling it twice writes nothing.  The record carries SHA-256 digests
    of each file before and after, never source text and never a diff, so it can be
    logged and streamed without a redaction pass.
    """
    run = _load_run(orchestrator, run_id)
    application = fix_application_stage.load_fix_application(run)
    if application is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Run {run_id} is in {run.current_state.value} and has no fix "
                f"application record: nothing has been written to this repository, "
                f"and nothing may be until a human approves the proposal."
            ),
        )
    return _fix_application_view(application)


@router.get(
    "/runs/{run_id}/verification",
    response_model=VerificationResult,
    summary="Get the Phase 11 verification record for a run",
    responses={
        200: {
            "description": (
                "The verification result, with its four claim booleans and the "
                "runner's exit code kept separate."
            )
        },
        400: {"description": "Malformed run ID."},
        404: {"description": "Unknown run."},
        409: {"description": "Verification has not been attempted for this run."},
    },
)
async def get_verification(
    run_id: str,
    orchestrator: MentorOrchestrator = Depends(orchestrator_dependency),
) -> VerificationResult:
    """Return what the executed test run actually established.

    ``tests_executed``, ``regression_check``, ``defect_demonstrated`` and
    ``fix_applied`` are four separate claims and are reported as four separate
    booleans; the server does not collapse them into a verdict.  A client can.  Every
    result carries a non-empty ``limitations`` list, and ``verified_file_hashes``
    binds it to the exact bytes that were on disk when the runner ran.
    """
    run = _load_run(orchestrator, run_id)
    verification = verification_stage.load_verification(run)
    if verification is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Run {run_id} is in {run.current_state.value} and has no verification "
                f"record: no test has been executed for this run, and nothing about "
                f"the repository has been verified."
            ),
        )
    return verification


# ── Phase 10: the outlined regression scenarios ───────────────────────────────


@router.get(
    "/runs/{run_id}/test-plan",
    response_model=TestPlanResponse,
    summary="Get the Phase 10 test plan for a run",
    responses={
        200: {
            "description": (
                "The plan. An honest decline is a 200 with no scenarios; coverage "
                "gaps no scenario closes are still reported."
            )
        },
        400: {"description": "Malformed run ID."},
        404: {"description": "Unknown run."},
        409: {"description": "The planning stage has not run for this run."},
    },
)
async def get_test_plan(
    run_id: str,
    orchestrator: MentorOrchestrator = Depends(orchestrator_dependency),
) -> TestPlanResponse:
    """Return the regression-test plan for a run.

    Today this endpoint answers 409 for every run the system produces, because the
    human approval gate stands in front of the planner: no run reaches ``TEST_PLAN``
    until a human has approved a diff.  A 200 is reachable only by approving and
    driving the run, which is exactly the point — a plan cannot be obtained by
    creating a run.

    ``tests_executed`` and ``test_source_written`` are hard-wired ``False`` and there
    is no run, apply or write endpoint anywhere near a plan.
    """
    run = _load_run(orchestrator, run_id)
    plan = load_test_plan(run)
    if plan is None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Run {run_id} is in {run.current_state.value} and has no test plan: "
                f"the TEST_PLAN stage has not run. Reaching it requires passing the "
                f"human approval gate first (POST /api/mentor/runs/{run_id}/approve-fix)."
            ),
        )
    return _test_plan_view(run, plan)


__all__ = [
    "ApproveFixRequest",
    "ApproveFixResponse",
    "CreateRunRequest",
    "FixApplicationResponse",
    "FixProposalResponse",
    "InvestigationResponse",
    "MentorRunResponse",
    "RootCauseResponse",
    "TestPlanResponse",
    "cancel_run",
    "create_run",
    "get_fix_application",
    "get_fix_proposal",
    "get_investigation",
    "get_root_cause",
    "get_run",
    "get_test_plan",
    "get_verification",
    "orchestrator_dependency",
    "router",
]
