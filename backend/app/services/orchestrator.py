"""
Workflow orchestrator — Phase 6, extended by Phase 11.

The Orchestrator owns the mentor workflow.  It is the *only* component allowed
to change a run's ``current_state`` or ``status``.

Responsibilities
----------------
* create runs with a server-generated run ID
* advance runs one stage at a time in deterministic order
* validate every state change through ``WorkflowStateMachine``
* record an append-only event for every meaningful step
* fail runs safely on stage errors
* expose run state and run event history for inspection
* own the one transition no stage may make for itself — approval of a fix

Scope
-----
The orchestrator drives whichever stages are registered and adds three things
Phase 11 needed that a stage cannot provide for itself:

* :meth:`MentorOrchestrator.approve_fix` — the *only* way into ``FIX_APPROVED``,
  and therefore the approval gate itself.  No stage is registered for
  ``FIX_APPROVED``, so ``advance()`` from the gate finds no handler and stalls:
  the workflow physically cannot write a file on its own.
* :meth:`MentorOrchestrator.approve_and_continue` — approve, then drive the run
  through ``FIX_APPLYING`` → ``FIX_APPLIED`` → ``TEST_PLAN`` → ``VERIFICATION``.
* Verdict resolution.  ``VERIFIED`` and ``VERIFICATION_FAILED`` are not stage
  states; they are what the recorded verification *result* means, so the
  orchestrator moves the run there after the verification stage has run.  That
  keeps ``VERIFIED`` unreachable except behind a real exit code.

A default run still walks only to ``FIX_PROPOSAL_READY`` on its own.  From there
it waits for a human, and nothing else.

A stage may publish progress events while it runs (Phase 7's investigation stage
does, once per investigator).  They are persisted, sequenced, streamed, and
returned in ``WorkflowAdvanceResult.events`` exactly like the events the
orchestrator emits itself.

Security
--------
* Clients can never set ``current_state`` or ``status`` — the HTTP layer does
  not accept those fields, and every transition here is server-computed from
  the state machine.
* Approval is bound to one exact proposal by content fingerprint.  The approval
  request carries no patch, no path, no target and no command, so there is
  nothing for a client to supply a different change with.
* Stages never execute repository code, never spawn subprocesses, never call
  ``eval``/``exec``, and never dynamically import repository modules, with one
  Phase 11 exception that is named here rather than left to be discovered: the
  *verification stage* runs the analysed repository's existing test files through
  the controlled executor in ``app.services.test_executor``, and the *fix
  application stage* writes the approved proposal to the repository.  Both are
  gated: the write behind an approval, and the execution behind the write.
  Everything before Phase 11 — the Phase 3/5/7/8 analysis and the Phase 9/10
  planning — remains read-only, and the Phase 10 planner reads nothing at all.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.models.fix_approval import proposal_fingerprint
from app.models.verification import VerificationStatus
from app.models.workflow import (
    MAX_ISSUE_LENGTH,
    MAX_REPOSITORY_PATH_LENGTH,
    MentorRun,
    RunStatus,
    StageOutcome,
    StageResult,
    WorkflowEvent,
    WorkflowEventType,
    WorkflowState,
    new_run_id,
    utc_now,
)
from app.services.event_store import EventStore
from app.services.fix_approval import (
    FixApprovalRejected,
    approval_context,
    build_approval,
)
from app.services.fix_proposal_stage import load_fix_proposal
from app.services.run_store import RunNotFoundError, RunStore
from app.services.stages import StageContext, StageHandler, build_default_stages
from app.services.state_machine import (
    NON_STAGE_STATES,
    SERVER_SIDE_STATES,
    InvalidTransitionError,
    WorkflowStateMachine,
)
from app.services.verification_stage import load_verification

logger = logging.getLogger(__name__)


class RunValidationError(Exception):
    """Raised when a run request is not acceptable.  Carries a safe reason."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class StageNotRegisteredError(Exception):
    """Raised when no handler is registered for the next workflow state."""

    def __init__(self, state: WorkflowState) -> None:
        self.state = state
        super().__init__(f"No stage handler registered for state {state.value}")


class WorkflowAdvanceResult(BaseModel):
    """The outcome of a single ``advance()`` call."""

    run: MentorRun
    events: list[WorkflowEvent] = Field(default_factory=list)
    #: True when the executed stage was an unimplemented placeholder, or the
    #: run was already terminal.
    stalled: bool = False
    #: True when the run moved to a new state.
    advanced: bool = False


# ── Orchestrator ──────────────────────────────────────────────────────────────


class MentorOrchestrator:
    """Deterministic, server-owned workflow coordinator."""

    def __init__(
        self,
        stages: dict[WorkflowState, StageHandler] | None = None,
        run_store: RunStore | None = None,
        event_store: EventStore | None = None,
        id_factory: Callable[[], str] = new_run_id,
    ) -> None:
        """
        Args:
            stages: Stage registry keyed by the state each stage serves.
                Defaults to :func:`app.services.stages.build_default_stages`.
                This is the injection point for later phases.
            run_store: Run persistence.  Defaults to a SQLite ``RunStore``.
            event_store: Append-only event log.  Defaults to a SQLite
                ``EventStore``.
            id_factory: Run ID generator.  Injected in tests to make run IDs
                predictable; defaults to ``uuid4``.

        Every collaborator is injectable so the orchestrator can be tested
        without a database and so later phases can swap in real agents.
        """
        self._stages: dict[WorkflowState, StageHandler] = (
            stages if stages is not None else build_default_stages()
        )
        self._runs: RunStore = run_store if run_store is not None else RunStore()
        self._events: EventStore = (
            event_store if event_store is not None else EventStore()
        )
        self._id_factory = id_factory

    # ── Run creation ──────────────────────────────────────────────────────

    def create_run(self, repository_path: str, issue: str) -> MentorRun:
        """Validate the request, create a run, and record ``RUN_CREATED``.

        Args:
            repository_path: Local repository path or reference.  Its shape is
                validated here; the Phase 3 ``RepositoryCloner`` validates the
                filesystem when the stage executes.
            issue: The developer's natural-language bug report or question.

        Raises:
            RunValidationError: If either field is empty, over-long, or
                contains NUL bytes.
        """
        _validate_repository_path(repository_path)
        _validate_issue(issue)

        now = utc_now()
        run = MentorRun(
            run_id=self._id_factory(),
            repository_path=repository_path.strip(),
            issue=issue.strip(),
            current_state=WorkflowState.CREATED,
            status=RunStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        self._runs.create(run)
        self._emit(
            run,
            WorkflowEventType.RUN_CREATED,
            f"Mentor run created for repository '{run.repository_path}'.",
            payload={
                "repository_path": run.repository_path,
                "issue": run.issue,
            },
        )
        logger.info(
            "Created mentor run %s in state %s", run.run_id, run.current_state.value
        )
        return run

    # ── Inspection ────────────────────────────────────────────────────────

    def get_run(self, run_id: str) -> MentorRun:
        """Return the run or raise ``RunNotFoundError``."""
        return self._runs.get(run_id)

    def list_runs(self, limit: int = 100) -> list[MentorRun]:
        """Return stored runs, newest first."""
        return self._runs.list_runs(limit=limit)

    def list_events(
        self, run_id: str, after_sequence: int = 0, limit: int = 500
    ) -> list[WorkflowEvent]:
        """Return this run's events, ordered by sequence.

        Raises:
            RunNotFoundError: If ``run_id`` is unknown, so an unknown run can
                never be used to probe another run's event stream.
        """
        if not self._runs.exists(run_id):
            raise RunNotFoundError(run_id)
        return self._events.list(run_id, after_sequence=after_sequence, limit=limit)

    def event_count(self, run_id: str) -> int:
        """Return the number of events recorded for this run."""
        return self._events.count(run_id)

    def latest_event_sequence(self, run_id: str) -> int:
        """Return the highest event sequence for this run (0 when empty)."""
        return self._events.latest_sequence(run_id)

    def tail_events(
        self, run_id: str, after_sequence: int = 0, limit: int = 200
    ) -> list[WorkflowEvent]:
        """Return this run's events after ``after_sequence``, without an
        existence pre-check.

        Used by the SSE stream, which has already validated the run.  Every
        query is still filtered on ``run_id``, so events can never leak across
        runs.
        """
        return self._events.list(run_id, after_sequence=after_sequence, limit=limit)

    def event_sequence_for(self, run_id: str, event_id: str) -> int | None:
        """Return the sequence of ``event_id`` within ``run_id``.

        Returns None when the event is unknown, which the SSE stream treats as
        "no resume cursor".
        """
        sequence = self._events.sequence_of(event_id)
        if sequence is None:
            return None
        # Guard against a cursor from a different run.
        event = self._events.get_by_event_id(event_id)
        if event is None or event.run_id != run_id:
            return None
        return sequence

    def registered_states(self) -> tuple[WorkflowState, ...]:
        """Return the states that currently have a handler."""
        return tuple(self._stages.keys())

    # ── Advancing ─────────────────────────────────────────────────────────

    def advance(self, run_id: str) -> WorkflowAdvanceResult:
        """Execute exactly one stage and apply its outcome.

        Outcomes:

        * the stage completes → the run moves to the next state
        * the stage is a placeholder → the run moves to the ``*_PENDING`` state
          and the result is reported as ``stalled``
        * no handler is registered for the next state → the run does not move
          and the result is reported as ``stalled`` (a wiring gap is not a
          workflow error, so the run is never failed for it)
        * the stage raises or fails → the run moves to ``FAILED``, or to the
          stage's declared ``failure_state`` when that is a reachable failure
          state

        The verification stage gets one extra step: completing it does not simply
        park the run in ``VERIFICATION``.  ``VERIFIED`` and
        ``VERIFICATION_FAILED`` are not stage states — they are what the recorded
        result *means* — so the orchestrator resolves the verdict and moves the run
        on.  See :meth:`_resolve_verification`.

        Never raises for stage failures.  Only unknown run IDs propagate
        (``RunNotFoundError``).
        """
        run = self._runs.get(run_id)
        emitted: list[WorkflowEvent] = []

        if WorkflowStateMachine.is_terminal(run.current_state):
            logger.info(
                "Ignoring advance() for terminal run %s in state %s",
                run_id,
                run.current_state.value,
            )
            return WorkflowAdvanceResult(run=run, events=[], stalled=True)

        current = run.current_state
        target = WorkflowStateMachine.next_state(current)
        if target is None:  # pragma: no cover - every non-terminal state has one
            return self._fail(run, "No forward transition available", emitted)

        handler = self._stages.get(target)
        if handler is None:
            return self._stall_on_unhandled_state(run, current, target, run_id)

        # ── Mark the run RUNNING, then announce the stage ─────────────────
        self._set_status(run, RunStatus.RUNNING)
        emitted.append(
            self._emit(
                run,
                WorkflowEventType.STAGE_STARTED,
                f"Stage {target.value} started.",
                payload={"stage": target.value, "from_state": current.value},
            )
        )

        context = StageContext(
            run=run,
            evidence=list(run.evidence),
            data=dict(run.context),
            # Progress events a stage publishes while it runs.  Appended to the
            # same list as the stage's own result events so the per-run sequence
            # stays gap-free and ordered, and so they are persisted and streamed
            # exactly like any other event.
            emit=lambda event_type, message, payload: self._emit_progress(
                run, emitted, event_type, message, payload
            ),
        )

        try:
            result = handler.execute(context)
        except Exception as exc:  # noqa: BLE001 - fail safely, never silently
            logger.exception("Stage %s raised for run %s", target.value, run_id)
            return self._fail(
                run,
                f"Stage {target.value} raised {type(exc).__name__}: {exc}",
                emitted,
                stage=(target.value, f"{type(exc).__name__}: {exc}"),
            )

        if not isinstance(result, StageResult):
            detail = f"returned {type(result).__name__}, expected StageResult"
            return self._fail(
                run,
                f"Stage {target.value} {detail}",
                emitted,
                stage=(target.value, detail),
            )

        # ── Dispatch on the honest stage outcome ─────────────────────────
        if result.outcome is StageOutcome.FAILED:
            return self._fail_from_result(run, target, result, emitted)

        if result.outcome is StageOutcome.PENDING:
            return self._settle_pending(run, current, target, result, emitted)

        settled = self._settle_completed(run, current, target, result, emitted)
        if (
            target is WorkflowState.VERIFICATION
            and not settled.stalled
            and settled.run.current_state is WorkflowState.VERIFICATION
        ):
            return self._resolve_verification(settled.run, target, settled.events)
        return settled

    def run_until_stalled(
        self, run_id: str, max_steps: int | None = None
    ) -> WorkflowAdvanceResult:
        """Advance repeatedly until nothing can advance the run any further.

        Deterministic: the same run always walks the same path and stops at the
        same state.  Three things end the walk, and none of them is "the stages
        ran out":

        * a placeholder stage stalls, which is where a real run stops today —
          ``FIX_PROPOSAL_READY``, waiting for a human;
        * a handler-less state that is not a stage, so advancing again would
          manufacture a "stalled" step;
        * a terminal state.

        The second one is why a verified run is not reported as stalled.  Walking
        past ``VERIFIED`` would target ``COMPLETED``, find no stage, and record a
        ``STAGE_PENDING`` event on a run that is one call from a perfectly good
        completion.
        """
        limit = (
            max_steps
            if max_steps is not None
            else len(WorkflowStateMachine.all_states()) + 1
        )
        if limit < 1:
            raise RunValidationError("max_steps must be at least 1")

        all_events: list[WorkflowEvent] = []
        last = WorkflowAdvanceResult(run=self._runs.get(run_id))

        for _ in range(limit):
            if self._advance_would_find_no_stage(run_id):
                # The run arrived somewhere the server finishes itself.  That is
                # not a stall: nothing is pending and nothing is missing, so the
                # result says so rather than borrowing the gate's word.
                last = WorkflowAdvanceResult(
                    run=self._runs.get(run_id), events=[], stalled=False
                )
                break
            last = self.advance(run_id)
            all_events.extend(last.events)
            if last.stalled or WorkflowStateMachine.is_terminal(
                last.run.current_state
            ):
                break

        return WorkflowAdvanceResult(
            run=last.run,
            events=all_events,
            stalled=last.stalled,
            advanced=last.advanced,
        )

    def _advance_would_find_no_stage(self, run_id: str) -> bool:
        """True when the run's next step is a state the server finishes itself.

        Checked before advancing rather than after, because the "no handler"
        answer is the correct behaviour but a *noisy* one: it emits a
        ``STAGE_PENDING`` event that reads as a stall.  On a verified run that
        would be a stall reported immediately before a successful completion, so
        the walk asks first.

        ``FIX_APPROVED`` is deliberately **not** in this set.  A run waiting at the
        gate *should* say so — loudly, and in its own vocabulary — so it goes
        through :meth:`advance` and gets a ``FIX_APPROVAL_REQUIRED`` event.
        """
        run = self._runs.get(run_id)
        if WorkflowStateMachine.is_terminal(run.current_state):
            return True
        target = WorkflowStateMachine.next_state(run.current_state)
        return target is None or target in SERVER_SIDE_STATES

    def _approval_required_payload(
        self,
        run: MentorRun,
        state: WorkflowState,
        proposal,
    ) -> dict[str, Any]:
        """The ``FIX_APPROVAL_REQUIRED`` payload, shared by both gate paths.

        Identical whether the run has just *arrived* at the gate or has been
        asked to advance again while already waiting there, so a client cannot
        tell the two apart — which is the point: the answer to "what is this run
        waiting for?" does not depend on how many times it was asked.
        """
        reviewable = proposal is not None and proposal.is_ready_for_review()
        return {
            "state": state.value,
            "awaiting_human_approval": True,
            "approval_endpoint": f"/api/mentor/runs/{run.run_id}/approve-fix",
            "proposal_fingerprint": (
                proposal_fingerprint(proposal) if reviewable else None
            ),
            "proposal_status": (
                proposal.status.value if proposal is not None else None
            ),
            "changed_repository": False,
        }

    def _stall_on_unhandled_state(
        self,
        run: MentorRun,
        current: WorkflowState,
        target: WorkflowState,
        run_id: str,
    ) -> WorkflowAdvanceResult:
        """Stop a run whose next state has no stage, saying honestly why.

        Three different situations land here and they are not the same news:

        * ``FIX_APPROVED`` — the gate.  The software is working; it is waiting for
          a person.  Reported as ``FIX_APPROVAL_REQUIRED``.
        * ``VERIFIED`` / ``COMPLETED`` — the run has arrived.  Nothing is pending,
          so there is nothing to report and nothing to apologise for.
        * anything else — a genuine wiring gap, logged as an error, because a
          missing stage for an ordinary state means the phase that owns it was
          never installed.

        In all three the run stays exactly where it is.  Stalling is the safe
        answer: failing the run would discard real evidence over a missing
        dictionary entry, and advancing anyway is what the state machine exists to
        prevent.
        """
        reason = str(StageNotRegisteredError(target))

        if target is WorkflowState.FIX_APPROVED:
            proposal = load_fix_proposal(run)
            logger.info(
                "Run %s is waiting at the approval gate for %s",
                run_id,
                ", ".join(proposal.affected_files) if proposal else "no recorded file",
            )
            return WorkflowAdvanceResult(
                run=run,
                events=[
                    self._emit(
                        run,
                        WorkflowEventType.FIX_APPROVAL_REQUIRED,
                        (
                            f"A fix proposal is waiting for human approval in "
                            f"{current.value}. Nothing will be written until a "
                            f"reviewer approves it by fingerprint."
                        ),
                        payload=self._approval_required_payload(run, current, proposal),
                    )
                ],
                stalled=True,
            )

        if target in NON_STAGE_STATES:
            logger.info(
                "%s for run %s, which is expected: no stage owns %s.",
                reason,
                run_id,
                target.value,
            )
            return WorkflowAdvanceResult(
                run=run,
                events=[
                    self._emit(
                        run,
                        WorkflowEventType.STAGE_PENDING,
                        (
                            f"{target.value} is not a stage state, so there is no "
                            f"further step to run. The run is stopped in "
                            f"{current.value}."
                        ),
                        payload={
                            "stage": target.value,
                            "implemented": False,
                            "by_design": True,
                            "reason": reason,
                            "current_state": current.value,
                        },
                    )
                ],
                stalled=True,
            )

        logger.error("%s for run %s", reason, run_id)
        return WorkflowAdvanceResult(
            run=run,
            events=[
                self._emit(
                    run,
                    WorkflowEventType.STAGE_PENDING,
                    (
                        f"No stage handler is registered for {target.value}. "
                        f"The run is stalled in {current.value}."
                    ),
                    payload={
                        "stage": target.value,
                        "implemented": False,
                        "by_design": False,
                        "reason": reason,
                        "current_state": current.value,
                    },
                )
            ],
            stalled=True,
        )

    # ── The approval gate ─────────────────────────────────────────────────

    def approve_fix(
        self,
        run_id: str,
        approved_by: str,
        comment: str = "",
        expected_fingerprint: str | None = None,
    ) -> WorkflowAdvanceResult:
        """Record a human's approval of one exact proposal and open the gate.

        This is the only way a run reaches ``FIX_APPROVED``, and it is
        server-side for the same reason every other transition is: the gate must
        not be something a request body can walk past.

        What this accepts is a name, an optional note, and optionally the
        fingerprint the client believes it is approving.  What it does *not*
        accept is a diff, a path, a line, a target, a command, or any state.  The
        change that will be written is the one already stored on the run, and it is
        fingerprinted here and re-checked by the applier immediately before the
        write — so a proposal edited between approval and application is caught
        rather than applied.

        ``expected_fingerprint`` is a compare-and-swap guard for a client that
        rendered a diff, then had it change underneath it (another tab, a re-run).
        When supplied and wrong, the approval is refused: the human did not
        approve *this* diff.

        Raises:
            FixApprovalRejected: If the run is not waiting at the gate, no
                proposal is ready, the fingerprint does not match, or the
                reviewer details are unusable.
        """
        run = self._runs.get(run_id)

        if run.current_state is not WorkflowState.FIX_PROPOSAL_READY:
            raise FixApprovalRejected(
                f"A fix can only be approved while the run is waiting at "
                f"FIX_PROPOSAL_READY. This run is in {run.current_state.value}."
            )

        proposal = load_fix_proposal(run)
        if proposal is None:
            raise FixApprovalRejected(
                "No fix proposal is recorded for this run, so there is nothing to "
                "approve."
            )
        if not proposal.is_ready_for_review():
            raise FixApprovalRejected(
                f"The recorded proposal has status {proposal.status.value} and "
                f"carries no diff to review, so there is nothing to approve. "
                f"{proposal.rationale}"
            )

        try:
            approval = build_approval(
                run,
                proposal,
                approved_by=approved_by,
                comment=comment,
            )
        except ValueError as exc:  # pragma: no cover - build_approval raises its own
            raise FixApprovalRejected(str(exc)) from exc

        if expected_fingerprint is not None and (
            expected_fingerprint != approval.proposal_fingerprint
        ):
            raise FixApprovalRejected(
                "The proposal this request was approving is no longer the one on "
                "record, so the approval was refused. Re-read the diff and "
                "approve the current proposal."
            )

        previous = run.current_state
        run.context.update(approval_context(approval))
        run = self._transition(run, WorkflowState.FIX_APPROVED, RunStatus.PENDING)

        emitted = [
            self._emit_state_changed(
                run, previous, f"Fix approved by {approval.approved_by}."
            ),
            self._emit(
                run,
                WorkflowEventType.FIX_APPROVED,
                (
                    f"{approval.approved_by} approved the proposed fix for "
                    f"{', '.join(approval.affected_files)} "
                    f"({approval.change_count} change(s), "
                    f"+{approval.added_lines}/-{approval.removed_lines}). "
                    f"Nothing has been written yet."
                ),
                payload=approval.as_event_payload(),
            ),
        ]
        logger.info(
            "Run %s: fix approved by %s (fingerprint %s)",
            run_id,
            approval.approved_by,
            approval.proposal_fingerprint,
        )
        return WorkflowAdvanceResult(run=run, events=emitted, advanced=True)

    def approve_and_continue(
        self,
        run_id: str,
        approved_by: str,
        comment: str = "",
        expected_fingerprint: str | None = None,
        max_steps: int | None = None,
    ) -> WorkflowAdvanceResult:
        """Approve the fix, then drive the run to its terminal state.

        The combination the API's approve endpoint uses.  Splitting it this way
        keeps :meth:`approve_fix` usable on its own — a client that wants to show
        the diff again between approval and application can call it, apply later,
        and the state machine will not object.

        Returns the aggregate result of every stage that ran, so an SSE client sees
        the whole write-then-verify sequence from one response.

        Raises:
            FixApprovalRejected: From :meth:`approve_fix`.
        """
        approved = self.approve_fix(
            run_id,
            approved_by=approved_by,
            comment=comment,
            expected_fingerprint=expected_fingerprint,
        )
        advanced = self.run_until_stalled(run_id, max_steps=max_steps)

        all_events = [*approved.events, *advanced.events]
        run = advanced.run
        if run.current_state is WorkflowState.VERIFIED:
            run = self.complete_run(run_id)
            # complete_run's own events are already appended to the store; the
            # caller's list stops at the last one it can attribute to a stage.
            logger.info("Run %s completed after a verified result", run_id)

        return WorkflowAdvanceResult(
            run=run,
            events=all_events,
            stalled=advanced.stalled,
            advanced=True,
        )

    # ── Terminal transitions (server-side only) ───────────────────────────

    def complete_run(self, run_id: str) -> MentorRun:
        """Move a ``VERIFIED`` run to ``COMPLETED``.

        ``VERIFIED`` is the *only* state this may be called from, and reaching it
        requires a recorded verification result whose runner exited zero.  That
        is the whole point: ``COMPLETED`` means "a real test run passed", not "the
        workflow ran out of stages".  Phase 6 could not offer this at all, because
        nothing could produce a verified result.

        Not reachable from ``advance()``: it is called by
        :meth:`approve_and_continue` after the run has settled in ``VERIFIED``.

        Raises:
            InvalidTransitionError: If the run is not in ``VERIFIED`` (including
                when it is already terminal).
        """
        run = self._runs.get(run_id)
        previous = run.current_state
        run = self._transition(run, WorkflowState.COMPLETED, RunStatus.COMPLETED)
        self._emit_state_changed(run, previous, "Run moved to COMPLETED.")
        self._emit(
            run,
            WorkflowEventType.RUN_COMPLETED,
            "Mentor run completed.",
            payload={"state": run.current_state.value},
        )
        return run

    def cancel_run(self, run_id: str) -> MentorRun:
        """Cancel a non-terminal run.

        Raises:
            InvalidTransitionError: If the run is already terminal.
        """
        run = self._runs.get(run_id)
        previous = run.current_state
        run = self._transition(run, WorkflowState.CANCELLED, RunStatus.CANCELLED)
        self._emit_state_changed(run, previous, "Run cancelled by operator request.")
        self._emit(
            run,
            WorkflowEventType.RUN_CANCELLED,
            "Mentor run cancelled.",
            payload={"state": run.current_state.value},
        )
        return run

    def fail_run(self, run_id: str, reason: str) -> MentorRun:
        """Move a non-terminal run to ``FAILED`` with an error message.

        Raises:
            InvalidTransitionError: If the run is already terminal.
        """
        run = self._runs.get(run_id)
        WorkflowStateMachine.assert_transition(run.current_state, WorkflowState.FAILED)
        return self._fail(run, reason, []).run

    # ── Verdict resolution ─────────────────────────────────────────────────

    def _resolve_verification(
        self,
        run: MentorRun,
        from_state: WorkflowState,
        emitted: list[WorkflowEvent],
    ) -> WorkflowAdvanceResult:
        """Move the run out of ``VERIFICATION`` according to the recorded result.

        ``VERIFIED`` and ``VERIFICATION_FAILED`` have no stage of their own on
        purpose.  A stage is "work the workflow did"; these are "what that work
        means", and letting a stage assert one directly would be the same mistake
        as letting a stage pick its own ``next_state``.  So the verification stage
        records a result and the orchestrator reads it.

        The mapping, and the reason each side is what it is:

        * ``PASSED`` → ``VERIFIED``.  The only path to a verified state, and it
          requires a runner that exited zero.
        * ``FAILED`` / ``TIMED_OUT`` → ``VERIFICATION_FAILED``.  The tool worked
          and the answer is no.  A timeout lands here rather than in ``FAILED``
          because nothing about the *tool* broke: it ran a suite, and the suite did
          not finish in the budget.  Calling that a broken workflow would be the
          one case where the distinction between "no" and "broken" actively
          misleads, because a developer would go looking for a bug in the tool.
        * ``BLOCKED`` / ``NOT_RUN`` / no result at all → ``FAILED``.  Nothing was
          concluded, and the run is left in a state whose name says so.

        Both failure-ish outcomes are terminal, so no answer is lost either way.
        """
        result = load_verification(run)

        if result is None:
            return self._fail(
                run,
                "The verification stage completed without recording a result, so "
                "the run cannot be resolved to a verdict.",
                emitted,
            )

        if result.status is VerificationStatus.PASSED:
            run = self._transition(
                run, WorkflowState.VERIFIED, RunStatus.PENDING
            )
            emitted.append(
                self._emit_state_changed(
                    run,
                    from_state,
                    "The controlled test run exited 0, so the applied change is "
                    "verified against the repository's existing suite.",
                )
            )
            return WorkflowAdvanceResult(
                run=run, events=emitted, stalled=False, advanced=True
            )

        if result.status in (
            VerificationStatus.FAILED,
            VerificationStatus.TIMED_OUT,
        ):
            reason = (
                result.findings[0]
                if result.findings
                else f"verification finished with status {result.status.value}"
            )
            run.error = reason
            run = self._transition(
                run, WorkflowState.VERIFICATION_FAILED, RunStatus.FAILED
            )
            emitted.append(
                self._emit_state_changed(
                    run,
                    from_state,
                    f"Verification finished {result.status.value}: {reason}",
                )
            )
            emitted.append(
                self._emit(
                    run,
                    WorkflowEventType.RUN_FAILED,
                    f"The applied change was not verified: {reason}",
                    payload={
                        "state": run.current_state.value,
                        "error": reason,
                        "verification_status": result.status.value,
                    },
                )
            )
            logger.info(
                "Run %s: verification finished %s", run.run_id, result.status.value
            )
            return WorkflowAdvanceResult(run=run, events=emitted, stalled=True)

        return self._fail(
            run,
            f"Verification reached no verdict (status {result.status.value}), so "
            f"the run cannot be resolved.",
            emitted,
        )

    # ── Outcome handlers ──────────────────────────────────────────────────

    def _settle_completed(
        self,
        run: MentorRun,
        current: WorkflowState,
        target: WorkflowState,
        result: StageResult,
        emitted: list[WorkflowEvent],
    ) -> WorkflowAdvanceResult:
        """Apply a successful stage result and move to ``target``."""
        # The state machine is authoritative.  A stage may declare where it
        # wanted to go, but it may not steer the workflow.
        if result.next_state is not None and result.next_state is not target:
            declared = result.next_state.value
            return self._fail(
                run,
                f"Stage {target.value} requested {declared}, but the workflow "
                f"only permits {target.value}",
                emitted,
            )

        run.evidence.extend(result.evidence)
        run.context.update(result.data)

        emitted.append(
            self._emit(
                run,
                WorkflowEventType.STAGE_COMPLETED,
                result.message,
                payload={
                    "stage": target.value,
                    "evidence_count": len(result.evidence),
                    **result.data,
                },
            )
        )
        run = self._transition(run, target, RunStatus.PENDING)
        emitted.append(
            self._emit_state_changed(
                run, current, f"Run advanced to {target.value}."
            )
        )
        return WorkflowAdvanceResult(
            run=run, events=emitted, stalled=False, advanced=True
        )

    def _settle_pending(
        self,
        run: MentorRun,
        current: WorkflowState,
        target: WorkflowState,
        result: StageResult,
        emitted: list[WorkflowEvent],
    ) -> WorkflowAdvanceResult:
        """Apply a placeholder stage result: record it, then wait.

        A ``*_PENDING`` state means "this stage is waiting to be implemented".
        Entering it is honest progress; leaving it is not possible because the
        next stage is also unimplemented.  No evidence is recorded, so a
        placeholder can never be mistaken for a finding.
        """
        if result.evidence:
            # Defensive: placeholder stages must never return evidence.
            logger.error(
                "Stage %s reported PENDING with %d evidence item(s); discarding",
                target.value,
                len(result.evidence),
            )

        emitted.append(
            self._emit(
                run,
                WorkflowEventType.STAGE_PENDING,
                result.message,
                payload={"stage": target.value, **result.data},
            )
        )
        run = self._transition(run, target, RunStatus.PENDING)
        emitted.append(
            self._emit_state_changed(
                run, current, f"Run reached {target.value} and is waiting."
            )
        )

        # Arriving *at* the gate is when the run first owes a human something, so
        # this is where the dedicated event belongs.  Without it a client polling
        # a run would only learn that approval is required after a second
        # advance() — and the event that means "waiting for a person" would only
        # ever appear on a run that was advanced a second time.
        #
        # Emitted only when there is something to approve.  A proposal that is
        # INSUFFICIENT_EVIDENCE or INVALID has no diff, so "approval required"
        # would be false, and the generic STAGE_PENDING above already says the
        # run is waiting.
        if target is WorkflowState.FIX_PROPOSAL_READY:
            proposal = load_fix_proposal(run)
            if proposal is not None and proposal.is_ready_for_review():
                emitted.append(
                    self._emit(
                        run,
                        WorkflowEventType.FIX_APPROVAL_REQUIRED,
                        (
                            f"A fix proposal for run {run.run_id} is waiting for "
                            f"human approval in {target.value}. Nothing will be "
                            f"written until a reviewer approves it by fingerprint."
                        ),
                        payload=self._approval_required_payload(
                            run, target, proposal
                        ),
                    )
                )

        return WorkflowAdvanceResult(run=run, events=emitted, stalled=True, advanced=True)

    def _fail_from_result(
        self,
        run: MentorRun,
        target: WorkflowState,
        result: StageResult,
        emitted: list[WorkflowEvent],
    ) -> WorkflowAdvanceResult:
        """Apply a failed stage result, honouring its declared failure state.

        Two things a plain ``_fail`` call cannot do:

        * **Keep what the stage learned.**  A Phase 11 stage can fail *and* have
          something to report — a blocked verification has a status, a reason and
          a limitations list.  Discarding that because the stage returned
          ``FAILED`` would make "we could not run the tests" indistinguishable
          from "we did not try", which is precisely the distinction this project
          exists to preserve.  The data is merged, and the evidence is kept.
        * **Choose between the two failure states.**  ``failure_state`` defaults to
          ``None``, which resolves to ``FAILED`` and is every pre-Phase-11
          stage's behaviour, untouched.  A stage that names
          ``VERIFICATION_FAILED`` gets the run there instead — but only after
          :meth:`WorkflowStateMachine.can_fail_to` confirms that is a real
          failure state *and* a legal edge from here, so ``failure_state`` is a
          choice of destination, never a back door.
        """
        error = result.error or result.message

        if result.evidence:
            run.evidence.extend(result.evidence)
        if result.data:
            run.context.update(result.data)

        destination = result.resolved_failure_state()
        if not WorkflowStateMachine.can_fail_to(run.current_state, destination):
            logger.error(
                "Stage %s asked to fail run %s out to %s, which is not a legal "
                "failure edge from %s; using FAILED",
                target.value,
                run.run_id,
                destination.value,
                run.current_state.value,
            )
            return self._fail(
                run,
                f"{error} (the stage requested the failure state "
                f"{destination.value}, which is not reachable from "
                f"{run.current_state.value}, so the run was failed instead)",
                emitted,
                stage=(target.value, error),
            )

        if destination is WorkflowState.FAILED:
            return self._fail(run, error, emitted, stage=(target.value, error))

        # VERIFICATION_FAILED: a working workflow that produced a negative answer.
        previous = run.current_state
        run.error = error
        run = self._transition(
            run, WorkflowState.VERIFICATION_FAILED, RunStatus.FAILED
        )
        emitted.append(
            self._emit(
                run,
                WorkflowEventType.STAGE_FAILED,
                f"Stage {target.value} failed: {error}",
                payload={"stage": target.value, "error": error},
            )
        )
        emitted.append(
            self._emit_state_changed(
                run, previous, f"Run failed: {error}"
            )
        )
        emitted.append(
            self._emit(
                run,
                WorkflowEventType.RUN_FAILED,
                f"Verification did not pass for run {run.run_id}: {error}",
                payload={
                    "state": run.current_state.value,
                    "error": error,
                    "stage": target.value,
                },
            )
        )
        logger.info(
            "Run %s ended in VERIFICATION_FAILED: %s", run.run_id, error
        )
        return WorkflowAdvanceResult(run=run, events=emitted, stalled=True)

    def _fail(
        self,
        run: MentorRun,
        reason: str,
        emitted: list[WorkflowEvent],
        stage: tuple[str, str] | None = None,
    ) -> WorkflowAdvanceResult:
        """Move a run to ``FAILED`` and record the failure events.

        ``stage`` is ``(stage_name, error_detail)`` when a stage actually ran;
        otherwise a generic ``STAGE_FAILED`` marker is recorded so that the run
        failure is always visible in the event log.
        """
        stage_name, error = stage if stage is not None else (None, reason)
        emitted.append(
            self._emit(
                run,
                WorkflowEventType.STAGE_FAILED,
                (
                    f"Stage {stage_name} failed: {error}"
                    if stage_name
                    else f"Run failed: {reason}"
                ),
                payload={"stage": stage_name, "error": error},
            )
        )

        previous = run.current_state
        if not WorkflowStateMachine.can_transition(previous, WorkflowState.FAILED):
            # Terminal run: nothing to do beyond reporting.
            logger.error(
                "Run %s cannot reach FAILED from terminal state %s",
                run.run_id,
                previous.value,
            )
            return WorkflowAdvanceResult(run=run, events=emitted, stalled=True)

        run.error = reason
        run = self._transition(run, WorkflowState.FAILED, RunStatus.FAILED)
        self._emit_state_changed(run, previous, f"Run failed: {reason}")
        self._emit(
            run,
            WorkflowEventType.RUN_FAILED,
            f"Mentor run failed: {reason}",
            payload={"error": reason, "state": run.current_state.value},
        )
        logger.warning("Mentor run %s failed: %s", run.run_id, reason)
        return WorkflowAdvanceResult(run=run, events=emitted, stalled=True)

    # ── private state/event writers ───────────────────────────────────────

    def _transition(
        self, run: MentorRun, target: WorkflowState, status: RunStatus
    ) -> MentorRun:
        """Validate and persist a state change.  The only writer of state."""
        WorkflowStateMachine.assert_transition(run.current_state, target)
        run.current_state = target
        run.status = status
        run.updated_at = utc_now()
        self._runs.save(run)
        return run

    def _set_status(self, run: MentorRun, status: RunStatus) -> None:
        if run.status == status:
            return
        run.status = status
        self._runs.save(run)

    def _emit(
        self,
        run: MentorRun,
        event_type: WorkflowEventType,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> WorkflowEvent:
        return self._events.append(
            run_id=run.run_id,
            event_type=event_type,
            state=run.current_state,
            status=run.status,
            message=message,
            payload=payload,
        )

    def _emit_progress(
        self,
        run: MentorRun,
        collected: list[WorkflowEvent],
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        """Persist and collect a progress event published by a running stage.

        ``event_type`` arrives as a string so ``stages.py`` does not have to know
        the event enum.  An unknown name is rejected here — a typo must fail
        loudly rather than be silently dropped from the event log.

        Returns:
            The event ID, which the stage may ignore.
        """
        try:
            resolved = WorkflowEventType(event_type)
        except ValueError:
            logger.error(
                "Stage %s published unknown event type %r for run %s",
                run.current_state.value,
                event_type,
                run.run_id,
            )
            raise

        event = self._emit(run, resolved, message, payload=payload)
        collected.append(event)
        logger.debug(
            "Run %s: progress event %s (%s)",
            run.run_id,
            resolved.value,
            event.sequence,
        )
        return event.event_id

    def _emit_state_changed(
        self, run: MentorRun, previous: WorkflowState, message: str
    ) -> WorkflowEvent:
        return self._emit(
            run,
            WorkflowEventType.STATE_CHANGED,
            message,
            payload={
                "from_state": previous.value,
                "to_state": run.current_state.value,
                "status": run.status.value,
            },
        )


# ── Default orchestrator ──────────────────────────────────────────────────────

_default_orchestrator: MentorOrchestrator | None = None


def get_default_orchestrator() -> MentorOrchestrator:
    """Return the process-wide orchestrator instance.

    A single instance is fine because every collaborator is stateless and
    SQLite-backed, so all API routes and the SSE stream observe the same data.
    """
    global _default_orchestrator
    if _default_orchestrator is None:
        _default_orchestrator = MentorOrchestrator()
    return _default_orchestrator


def reset_default_orchestrator() -> None:
    """Drop the cached orchestrator (used by the test suite)."""
    global _default_orchestrator
    _default_orchestrator = None


# ── Validation helpers ────────────────────────────────────────────────────────


def _validate_repository_path(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RunValidationError("repository_path must not be empty")
    if "\x00" in value:
        raise RunValidationError("repository_path must not contain NUL bytes")
    if len(value) > MAX_REPOSITORY_PATH_LENGTH:
        raise RunValidationError(
            f"repository_path must be at most {MAX_REPOSITORY_PATH_LENGTH} characters"
        )


def _validate_issue(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise RunValidationError("issue must not be empty")
    if "\x00" in value:
        raise RunValidationError("issue must not contain NUL bytes")
    if len(value) > MAX_ISSUE_LENGTH:
        raise RunValidationError(
            f"issue must be at most {MAX_ISSUE_LENGTH} characters"
        )


__all__ = [
    "FixApprovalRejected",
    "InvalidTransitionError",
    "MentorOrchestrator",
    "RunNotFoundError",
    "RunValidationError",
    "StageNotRegisteredError",
    "WorkflowAdvanceResult",
    "get_default_orchestrator",
    "reset_default_orchestrator",
]
