"""
Phase 6 tests — workflow state machine (extended by Phase 11).

The state machine is pure and has no I/O, so every rule is tested directly
without a database, an orchestrator, or an HTTP client.

Phase 11 changes three things here, and each has its own test below rather than
being folded into an existing assertion:

* the linear path grew the approval, application and verification states;
* ``VERIFICATION_PENDING`` was replaced by ``VERIFICATION``, unaliased;
* ``VERIFICATION_FAILED`` was added as a terminal state distinct from ``FAILED``.
"""

from __future__ import annotations

import pytest

from app.models.workflow import WorkflowState
from app.services.state_machine import (
    ALLOWED_TRANSITIONS,
    ANSWERED_STATES,
    LINEAR_PROGRESSION,
    TERMINAL_STATES,
    InvalidTransitionError,
    WorkflowStateMachine,
)


# ── Structure ─────────────────────────────────────────────────────────────────


def test_every_state_has_a_transition_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(WorkflowState)


def test_terminal_states_have_no_outgoing_transitions() -> None:
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()
        assert WorkflowStateMachine.is_terminal(state)


def test_expected_states_are_declared() -> None:
    assert {s.value for s in WorkflowState} == {
        "CREATED",
        "REPOSITORY_CONTEXT",
        "ARCHITECTURE_CONTEXT",
        "INVESTIGATION_PENDING",
        "EVIDENCE_PENDING",
        "ROOT_CAUSE_ANALYSIS",
        "ROOT_CAUSE_READY",
        "FIX_PROPOSAL",
        "FIX_PROPOSAL_READY",
        "FIX_APPROVED",
        "FIX_APPLYING",
        "FIX_APPLIED",
        "TEST_PLAN",
        "VERIFICATION",
        "VERIFIED",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "VERIFICATION_FAILED",
    }


def test_verification_pending_was_renamed_not_aliased() -> None:
    """``VERIFICATION_PENDING`` must not exist as a second name for the same thing.

    An alias would let a client keep saying the old word, and two names for one
    state is how a UI ends up showing a phase as unimplemented after it is not.
    """
    assert not hasattr(WorkflowState, "VERIFICATION_PENDING")
    assert WorkflowState.VERIFICATION.value == "VERIFICATION"


def test_linear_progression_is_the_documented_path() -> None:
    assert [s.value for s in LINEAR_PROGRESSION] == [
        "CREATED",
        "REPOSITORY_CONTEXT",
        "ARCHITECTURE_CONTEXT",
        "INVESTIGATION_PENDING",
        "EVIDENCE_PENDING",
        "ROOT_CAUSE_ANALYSIS",
        "ROOT_CAUSE_READY",
        "FIX_PROPOSAL",
        "FIX_PROPOSAL_READY",
        "FIX_APPROVED",
        "FIX_APPLYING",
        "FIX_APPLIED",
        "TEST_PLAN",
        "VERIFICATION",
        "VERIFIED",
        "COMPLETED",
    ]


def test_transition_table_is_exhaustive_and_symmetric_in_shape() -> None:
    """Every non-terminal state offers exactly one forward target."""
    for state in WorkflowState:
        if WorkflowStateMachine.is_terminal(state):
            continue
        forward = WorkflowStateMachine.next_state(state)
        assert forward is not None
        assert forward in WorkflowStateMachine.allowed_transitions(state)


# ── Valid transitions ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "expected_next"),
    [
        (WorkflowState.CREATED, WorkflowState.REPOSITORY_CONTEXT),
        (WorkflowState.REPOSITORY_CONTEXT, WorkflowState.ARCHITECTURE_CONTEXT),
        (
            WorkflowState.ARCHITECTURE_CONTEXT,
            WorkflowState.INVESTIGATION_PENDING,
        ),
        (WorkflowState.INVESTIGATION_PENDING, WorkflowState.EVIDENCE_PENDING),
        (WorkflowState.EVIDENCE_PENDING, WorkflowState.ROOT_CAUSE_ANALYSIS),
        (WorkflowState.ROOT_CAUSE_ANALYSIS, WorkflowState.ROOT_CAUSE_READY),
        (WorkflowState.ROOT_CAUSE_READY, WorkflowState.FIX_PROPOSAL),
        (WorkflowState.FIX_PROPOSAL, WorkflowState.FIX_PROPOSAL_READY),
        (WorkflowState.FIX_PROPOSAL_READY, WorkflowState.FIX_APPROVED),
        (WorkflowState.FIX_APPROVED, WorkflowState.FIX_APPLYING),
        (WorkflowState.FIX_APPLYING, WorkflowState.FIX_APPLIED),
        (WorkflowState.FIX_APPLIED, WorkflowState.TEST_PLAN),
        (WorkflowState.TEST_PLAN, WorkflowState.VERIFICATION),
        (WorkflowState.VERIFICATION, WorkflowState.VERIFIED),
        (WorkflowState.VERIFIED, WorkflowState.COMPLETED),
    ],
)
def test_valid_forward_transitions(
    current: WorkflowState, expected_next: WorkflowState
) -> None:
    assert WorkflowStateMachine.can_transition(current, expected_next)
    assert WorkflowStateMachine.next_state(current) is expected_next
    assert WorkflowStateMachine.is_forward_progression(current, expected_next)
    WorkflowStateMachine.assert_transition(current, expected_next)  # must not raise


@pytest.mark.parametrize(
    "state",
    [s for s in WorkflowState if s not in TERMINAL_STATES],
)
def test_any_non_terminal_state_may_fail(state: WorkflowState) -> None:
    assert WorkflowStateMachine.can_transition(state, WorkflowState.FAILED)
    WorkflowStateMachine.assert_transition(state, WorkflowState.FAILED)


@pytest.mark.parametrize(
    "state",
    [s for s in WorkflowState if s not in TERMINAL_STATES],
)
def test_any_non_terminal_state_may_be_cancelled(state: WorkflowState) -> None:
    assert WorkflowStateMachine.can_transition(state, WorkflowState.CANCELLED)
    WorkflowStateMachine.assert_transition(state, WorkflowState.CANCELLED)


# ── The approval gate ─────────────────────────────────────────────────────────


def test_the_gate_leads_only_to_approval() -> None:
    """``FIX_PROPOSAL_READY`` must not lead anywhere a fix could be written.

    The gate is a property of the *table*, not of a stage: if this edge ever grew
    a second target, some future caller would find a way through it.
    """
    forward = WorkflowStateMachine.allowed_transitions(
        WorkflowState.FIX_PROPOSAL_READY
    ) - {WorkflowState.FAILED, WorkflowState.CANCELLED}
    assert forward == frozenset({WorkflowState.FIX_APPROVED})


def test_nothing_may_skip_the_gate() -> None:
    """No state before the gate may reach the write path directly.

    Only the states that come *before* ``FIX_PROPOSAL_READY`` are checked.
    ``TEST_PLAN`` is deliberately not among them: Phase 11 moved it to *after*
    ``FIX_APPLIED``, so reaching ``VERIFICATION`` from it is the documented
    progression and not a skipped gate.
    """
    before_gate = list(
        LINEAR_PROGRESSION[: LINEAR_PROGRESSION.index(WorkflowState.FIX_PROPOSAL_READY)]
    )
    assert before_gate, "the gate must have states before it"
    for state in before_gate:
        allowed = WorkflowStateMachine.allowed_transitions(state)
        for forbidden in (
            WorkflowState.FIX_APPROVED,
            WorkflowState.FIX_APPLYING,
            WorkflowState.FIX_APPLIED,
            WorkflowState.TEST_PLAN,
            WorkflowState.VERIFICATION,
            WorkflowState.VERIFIED,
            WorkflowState.COMPLETED,
        ):
            assert forbidden not in allowed, f"{state.value} -> {forbidden.value}"


def test_only_verification_may_reach_a_verified_state() -> None:
    """``VERIFIED`` is reachable from exactly one place, and that place is a
    runner's exit code."""
    sources = {
        state
        for state in WorkflowState
        if WorkflowState.VERIFIED in WorkflowStateMachine.allowed_transitions(state)
    }
    assert sources == {WorkflowState.VERIFICATION}


# ── Failure-state classification ──────────────────────────────────────────────


def test_verification_failed_is_terminal_and_distinct_from_failed() -> None:
    assert WorkflowStateMachine.is_terminal(WorkflowState.VERIFICATION_FAILED)
    assert WorkflowState.VERIFICATION_FAILED is not WorkflowState.FAILED
    assert WorkflowState.VERIFICATION_FAILED in ANSWERED_STATES
    assert WorkflowState.FAILED not in ANSWERED_STATES
    assert WorkflowState.COMPLETED in ANSWERED_STATES


def test_is_failure_state_covers_exactly_two_members() -> None:
    assert WorkflowStateMachine.is_failure_state(WorkflowState.FAILED)
    assert WorkflowStateMachine.is_failure_state(WorkflowState.VERIFICATION_FAILED)
    for state in WorkflowState:
        if state in (WorkflowState.FAILED, WorkflowState.VERIFICATION_FAILED):
            continue
        assert not WorkflowStateMachine.is_failure_state(state), state.value


def test_only_verification_may_fail_into_verification_failed() -> None:
    """A stage anywhere else cannot claim the "answered no" terminal state."""
    for state in WorkflowState:
        expected = state is WorkflowState.VERIFICATION
        assert WorkflowStateMachine.can_fail_to(
            state, WorkflowState.VERIFICATION_FAILED
        ) is expected, state.value


def test_a_stage_cannot_use_failure_state_as_a_back_door() -> None:
    """``can_fail_to`` refuses anything that is not a failure state, even when
    the transition table would permit it."""
    for state in WorkflowState:
        if WorkflowStateMachine.is_terminal(state):
            continue
        for target in WorkflowStateMachine.allowed_transitions(state):
            if target in (WorkflowState.FAILED, WorkflowState.VERIFICATION_FAILED):
                continue
            assert not WorkflowStateMachine.can_fail_to(state, target)


# ── Invalid transitions ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("current", "requested"),
    [
        (WorkflowState.CREATED, WorkflowState.COMPLETED),
        (WorkflowState.CREATED, WorkflowState.ARCHITECTURE_CONTEXT),
        (WorkflowState.CREATED, WorkflowState.INVESTIGATION_PENDING),
        (WorkflowState.REPOSITORY_CONTEXT, WorkflowState.CREATED),
        (WorkflowState.ARCHITECTURE_CONTEXT, WorkflowState.REPOSITORY_CONTEXT),
        (WorkflowState.INVESTIGATION_PENDING, WorkflowState.FIX_PROPOSAL),
        (WorkflowState.EVIDENCE_PENDING, WorkflowState.TEST_PLAN),
        (WorkflowState.EVIDENCE_PENDING, WorkflowState.ROOT_CAUSE_READY),
        (WorkflowState.ROOT_CAUSE_ANALYSIS, WorkflowState.FIX_PROPOSAL_READY),
        (WorkflowState.ROOT_CAUSE_READY, WorkflowState.TEST_PLAN),
        # Phase 9 stops at its own gate; Phase 11 did not move it.
        (WorkflowState.FIX_PROPOSAL_READY, WorkflowState.COMPLETED),
        (WorkflowState.FIX_PROPOSAL_READY, WorkflowState.VERIFICATION),
        (WorkflowState.FIX_PROPOSAL_READY, WorkflowState.TEST_PLAN),
        (WorkflowState.FIX_APPROVED, WorkflowState.TEST_PLAN),
        (WorkflowState.FIX_APPLYING, WorkflowState.VERIFICATION),
        (WorkflowState.FIX_APPLIED, WorkflowState.VERIFICATION),
        (WorkflowState.TEST_PLAN, WorkflowState.COMPLETED),
        (WorkflowState.VERIFICATION, WorkflowState.COMPLETED),
        (WorkflowState.VERIFICATION, WorkflowState.TEST_PLAN),
        (WorkflowState.VERIFICATION, WorkflowState.FIX_APPLIED),
    ],
)
def test_invalid_transitions_are_rejected(
    current: WorkflowState, requested: WorkflowState
) -> None:
    assert not WorkflowStateMachine.can_transition(current, requested)
    with pytest.raises(InvalidTransitionError) as excinfo:
        WorkflowStateMachine.assert_transition(current, requested)
    assert excinfo.value.current is current
    assert excinfo.value.requested is requested
    assert current.value in str(excinfo.value)
    assert requested.value in str(excinfo.value)


def test_skipping_states_is_rejected() -> None:
    """A run may not jump straight from CREATED to COMPLETED."""
    with pytest.raises(InvalidTransitionError):
        WorkflowStateMachine.assert_transition(
            WorkflowState.CREATED, WorkflowState.COMPLETED
        )


def test_self_transitions_are_rejected() -> None:
    for state in WorkflowState:
        if WorkflowStateMachine.is_terminal(state):
            continue
        assert not WorkflowStateMachine.can_transition(state, state)


# ── Terminal state protection ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "terminal",
    [
        WorkflowState.COMPLETED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
        WorkflowState.VERIFICATION_FAILED,
    ],
)
def test_terminal_states_cannot_transition_to_anything(terminal: WorkflowState) -> None:
    for target in WorkflowState:
        assert not WorkflowStateMachine.can_transition(terminal, target)
        with pytest.raises(InvalidTransitionError):
            WorkflowStateMachine.assert_transition(terminal, target)


@pytest.mark.parametrize(
    "terminal",
    [
        WorkflowState.COMPLETED,
        WorkflowState.FAILED,
        WorkflowState.CANCELLED,
        WorkflowState.VERIFICATION_FAILED,
    ],
)
def test_terminal_states_have_no_next_state(terminal: WorkflowState) -> None:
    assert WorkflowStateMachine.next_state(terminal) is None


def test_is_terminal_only_for_settled_states() -> None:
    assert not WorkflowStateMachine.is_terminal(WorkflowState.CREATED)
    assert not WorkflowStateMachine.is_terminal(WorkflowState.VERIFICATION)
    assert not WorkflowStateMachine.is_terminal(WorkflowState.VERIFIED)
    assert WorkflowStateMachine.is_terminal(WorkflowState.COMPLETED)
    assert WorkflowStateMachine.is_terminal(WorkflowState.VERIFICATION_FAILED)


# ── Determinism ───────────────────────────────────────────────────────────────


def test_machine_is_deterministic() -> None:
    """Repeated queries return identical answers — no clock, no randomness."""
    expected_next = {
        current: following
        for current, following in zip(LINEAR_PROGRESSION, LINEAR_PROGRESSION[1:])
    }
    for _ in range(5):
        for state in WorkflowState:
            assert (
                WorkflowStateMachine.allowed_transitions(state)
                == ALLOWED_TRANSITIONS[state]
            )
            assert WorkflowStateMachine.next_state(state) == expected_next.get(state)


def test_machine_methods_do_not_require_an_instance() -> None:
    assert WorkflowStateMachine.is_terminal(WorkflowState.FAILED)
    assert WorkflowStateMachine.can_transition(
        WorkflowState.CREATED, WorkflowState.FAILED
    )


def test_all_states_returns_every_state() -> None:
    assert set(WorkflowStateMachine.all_states()) == set(WorkflowState)
