"""
Workflow state machine — Phase 6, extended by Phase 11.

A pure, dependency-free, deterministic description of the mentor workflow.
It has no I/O, no clock reads, no randomness and no LLM dependency, so every
rule below is directly unit-testable in isolation.

Linear progression
------------------

    CREATED
      → REPOSITORY_CONTEXT
      → ARCHITECTURE_CONTEXT
      → INVESTIGATION_PENDING
      → EVIDENCE_PENDING
      → ROOT_CAUSE_ANALYSIS
      → ROOT_CAUSE_READY
      → FIX_PROPOSAL
      → FIX_PROPOSAL_READY
      → FIX_APPROVED
      → FIX_APPLYING
      → FIX_APPLIED
      → TEST_PLAN
      → VERIFICATION
      → VERIFIED
      → COMPLETED

Escape hatches
--------------

    Any non-terminal state → FAILED
    Any non-terminal state → CANCELLED

    VERIFICATION → VERIFICATION_FAILED
    VERIFICATION → FAILED

COMPLETED, FAILED, CANCELLED and VERIFICATION_FAILED are terminal.
"""

from __future__ import annotations

import logging
from typing import Final

from app.models.workflow import WorkflowState

logger = logging.getLogger(__name__)


class InvalidTransitionError(Exception):
    """Raised when a requested state transition is not permitted."""

    def __init__(
        self,
        current: WorkflowState,
        requested: WorkflowState,
        allowed: frozenset["WorkflowState"],
    ) -> None:
        self.current = current
        self.requested = requested
        self.allowed = allowed
        allowed_text = ", ".join(sorted(s.value for s in allowed)) or "<none>"
        super().__init__(
            f"Invalid workflow transition {current.value} -> {requested.value}. "
            f"Allowed from {current.value}: {allowed_text}"
        )


#: States with no outgoing transitions.
TERMINAL_STATES: Final[frozenset[WorkflowState]] = frozenset(
    {
        WorkflowState.COMPLETED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
        WorkflowState.VERIFICATION_FAILED,
    }
)


#: The single forward path through the workflow, in order.
LINEAR_PROGRESSION: Final[tuple[WorkflowState, ...]] = (
    WorkflowState.CREATED,
    WorkflowState.REPOSITORY_CONTEXT,
    WorkflowState.ARCHITECTURE_CONTEXT,
    WorkflowState.INVESTIGATION_PENDING,
    WorkflowState.EVIDENCE_PENDING,
    WorkflowState.ROOT_CAUSE_ANALYSIS,
    WorkflowState.ROOT_CAUSE_READY,
    WorkflowState.FIX_PROPOSAL,
    WorkflowState.FIX_PROPOSAL_READY,
    WorkflowState.FIX_APPROVED,
    WorkflowState.FIX_APPLYING,
    WorkflowState.FIX_APPLIED,
    WorkflowState.TEST_PLAN,
    WorkflowState.VERIFICATION,
    WorkflowState.VERIFIED,
    WorkflowState.COMPLETED,
)


#: States the run reaches and leaves only through an explicit server-side call.
SERVER_SIDE_STATES: Final[frozenset[WorkflowState]] = frozenset(
    {
        WorkflowState.VERIFIED,
        WorkflowState.COMPLETED,
    }
)


#: States whose meaning is asserted by the orchestrator rather than produced
#: by a stage.
NON_STAGE_STATES: Final[frozenset[WorkflowState]] = frozenset(
    {WorkflowState.FIX_APPROVED}
) | SERVER_SIDE_STATES


#: States that mean the run has left the forward path and can only be reported.
ANSWERED_STATES: Final[frozenset[WorkflowState]] = frozenset(
    {WorkflowState.COMPLETED, WorkflowState.VERIFICATION_FAILED}
)


#: Explicit, exhaustive transition table.
ALLOWED_TRANSITIONS: Final[dict[WorkflowState, frozenset[WorkflowState]]] = {
    WorkflowState.CREATED: frozenset(
        {
            WorkflowState.REPOSITORY_CONTEXT,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.REPOSITORY_CONTEXT: frozenset(
        {
            WorkflowState.ARCHITECTURE_CONTEXT,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.ARCHITECTURE_CONTEXT: frozenset(
        {
            WorkflowState.INVESTIGATION_PENDING,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.INVESTIGATION_PENDING: frozenset(
        {
            WorkflowState.EVIDENCE_PENDING,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.EVIDENCE_PENDING: frozenset(
        {
            WorkflowState.ROOT_CAUSE_ANALYSIS,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.ROOT_CAUSE_ANALYSIS: frozenset(
        {
            WorkflowState.ROOT_CAUSE_READY,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.ROOT_CAUSE_READY: frozenset(
        {
            WorkflowState.FIX_PROPOSAL,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.FIX_PROPOSAL: frozenset(
        {
            WorkflowState.FIX_PROPOSAL_READY,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    # Approval gate. FIX_APPROVED is reachable only through the
    # orchestrator's approve_fix().
    WorkflowState.FIX_PROPOSAL_READY: frozenset(
        {
            WorkflowState.FIX_APPROVED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.FIX_APPROVED: frozenset(
        {
            WorkflowState.FIX_APPLYING,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.FIX_APPLYING: frozenset(
        {
            WorkflowState.FIX_APPLIED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.FIX_APPLIED: frozenset(
        {
            WorkflowState.TEST_PLAN,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.TEST_PLAN: frozenset(
        {
            WorkflowState.VERIFICATION,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.VERIFICATION: frozenset(
        {
            WorkflowState.VERIFIED,
            WorkflowState.VERIFICATION_FAILED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.VERIFIED: frozenset(
        {
            WorkflowState.COMPLETED,
            WorkflowState.FAILED,
            WorkflowState.CANCELLED,
        }
    ),
    WorkflowState.COMPLETED: frozenset(),
    WorkflowState.FAILED: frozenset(),
    WorkflowState.CANCELLED: frozenset(),
    WorkflowState.VERIFICATION_FAILED: frozenset(),
}


class WorkflowStateMachine:
    """Stateless helper exposing the transition table."""

    # Public view required by Phase 11 structural checks.
    # The actual transition data remains owned by ALLOWED_TRANSITIONS.
    transitions = ALLOWED_TRANSITIONS

    @staticmethod
    def all_states() -> tuple[WorkflowState, ...]:
        """Return every declared workflow state."""
        return tuple(WorkflowState)

    @staticmethod
    def is_terminal(state: WorkflowState) -> bool:
        """Return True when state has no outgoing transitions."""
        return state in TERMINAL_STATES

    @staticmethod
    def allowed_transitions(
        state: WorkflowState,
    ) -> frozenset[WorkflowState]:
        """Return every state reachable from state in one step."""
        return ALLOWED_TRANSITIONS[state]

    @staticmethod
    def can_transition(
        current: WorkflowState,
        target: WorkflowState,
    ) -> bool:
        """Return True when current → target is permitted."""
        return target in ALLOWED_TRANSITIONS[current]

    @staticmethod
    def assert_transition(
        current: WorkflowState,
        target: WorkflowState,
    ) -> None:
        """Raise InvalidTransitionError unless the transition is allowed."""
        if not WorkflowStateMachine.can_transition(current, target):
            allowed = WorkflowStateMachine.allowed_transitions(current)
            logger.warning(
                "Rejected workflow transition %s -> %s",
                current.value,
                target.value,
            )
            raise InvalidTransitionError(current, target, allowed)

    @staticmethod
    def next_state(
        current: WorkflowState,
    ) -> WorkflowState | None:
        """Return the single forward state, or None at/after COMPLETED."""
        try:
            index = LINEAR_PROGRESSION.index(current)
        except ValueError:
            return None

        if index + 1 >= len(LINEAR_PROGRESSION):
            return None

        return LINEAR_PROGRESSION[index + 1]

    @staticmethod
    def is_forward_progression(
        current: WorkflowState,
        target: WorkflowState,
    ) -> bool:
        """Return True when target is the forward state after current."""
        return WorkflowStateMachine.next_state(current) == target

    @staticmethod
    def is_failure_state(state: WorkflowState) -> bool:
        """Return True when state is an allowed failure state."""
        return state in (
            WorkflowState.FAILED,
            WorkflowState.VERIFICATION_FAILED,
        )

    @staticmethod
    def can_fail_to(
        current: WorkflowState,
        target: WorkflowState,
    ) -> bool:
        """Return True when a stage may fail current out to target."""
        return (
            WorkflowStateMachine.is_failure_state(target)
            and target in ALLOWED_TRANSITIONS[current]
        )