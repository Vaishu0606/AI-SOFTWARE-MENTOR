"""
Phase 6 tests — the workflow orchestrator.

Covers run creation, deterministic advancement, failure handling, terminal
state protection, event recording, and — importantly — the guarantee that
placeholder stages never fabricate findings.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from app.models.domain import Evidence, EvidenceType
from app.models.fix_proposal import FixChange, FixProposal, FixProposalStatus
from app.models.test_plan import TestExecutionStatus
from app.models.verification import VerificationStatus
from app.models.workflow import (
    MentorRun,
    RunStatus,
    StageOutcome,
    StageResult,
    WorkflowEventType,
    WorkflowState,
    is_valid_run_id,
)
from app.services.orchestrator import (
    MentorOrchestrator,
    RunNotFoundError,
    RunValidationError,
)
from app.services.run_store import RunStore
from app.services.stages import (
    PlaceholderStage,
    StageContext,
    StageHandler,
    build_default_stages,
)
from app.services.state_machine import InvalidTransitionError, WorkflowStateMachine
from app.services.test_plan_stage import load_test_plan
from app.services.test_executor import ControlledTestExecutor
from app.services.verification_stage import (
    VERIFIER_AGENT,
    counterfactual_discrimination,
    load_verification,
)
from tests.conftest import BUGGY_DISCOUNT, FIXED_DISCOUNT

ISSUE = "Checkout is failing when a discount code is applied."

#: The three Phase 7 investigator agents, used to scope evidence assertions so
#: that the Phase 3/5 stages keep their own (already tested) contract.
INVESTIGATOR_AGENTS = {
    "code_investigator",
    "test_investigator",
    "dependency_investigator",
}


@pytest.fixture
def orchestrator() -> MentorOrchestrator:
    return MentorOrchestrator()


def _snapshot(root: Path) -> dict[str, str]:
    """Hash every file under ``root`` so modifications can be detected."""
    out: dict[str, str] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            path = Path(dirpath) / name
            out[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return out


# ── Run creation ──────────────────────────────────────────────────────────────


def test_create_run_returns_a_persisted_run(orchestrator: MentorOrchestrator) -> None:
    run = orchestrator.create_run("C:/repo", ISSUE)
    assert is_valid_run_id(run.run_id)
    assert run.repository_path == "C:/repo"
    assert run.issue == ISSUE
    assert orchestrator.get_run(run.run_id).run_id == run.run_id


def test_new_runs_start_in_created_and_pending(orchestrator: MentorOrchestrator) -> None:
    run = orchestrator.create_run("C:/repo", ISSUE)
    assert run.current_state is WorkflowState.CREATED
    assert run.status is RunStatus.PENDING
    assert run.error is None
    assert run.evidence == []


def test_create_run_sets_timestamps(orchestrator: MentorOrchestrator) -> None:
    run = orchestrator.create_run("C:/repo", ISSUE)
    assert run.created_at is not None
    assert run.updated_at is not None
    assert run.updated_at >= run.created_at


def test_run_ids_are_unique(orchestrator: MentorOrchestrator) -> None:
    ids = {orchestrator.create_run("C:/repo", ISSUE).run_id for _ in range(50)}
    assert len(ids) == 50
    assert all(is_valid_run_id(i) for i in ids)


def test_injected_id_factory_is_used(sample_repo: Path) -> None:
    fixed = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    ]
    orch = MentorOrchestrator(id_factory=lambda: fixed.pop(0))
    first = orch.create_run(str(sample_repo), ISSUE)
    second = orch.create_run(str(sample_repo), ISSUE)
    assert first.run_id != second.run_id
    assert first.run_id == "11111111-1111-4111-8111-111111111111"


def test_repository_reference_is_trimmed(orchestrator: MentorOrchestrator) -> None:
    run = orchestrator.create_run("  C:/repo  ", "  " + ISSUE + "  ")
    assert run.repository_path == "C:/repo"
    assert run.issue == ISSUE


@pytest.mark.parametrize(
    ("path", "issue"),
    [
        ("", ISSUE),
        ("   ", ISSUE),
        (None, ISSUE),  # type: ignore[arg-type]
        ("C:/repo", ""),
        ("C:/repo", "   "),
        (None, None),  # type: ignore[arg-type]
    ],
)
def test_create_run_rejects_empty_fields(
    orchestrator: MentorOrchestrator, path: str, issue: str
) -> None:
    with pytest.raises(RunValidationError):
        orchestrator.create_run(path, issue)


@pytest.mark.parametrize("bad", ["C:/repo\x00evil", "x" * 2000])
def test_create_run_rejects_unsafe_repository_reference(
    orchestrator: MentorOrchestrator, bad: str
) -> None:
    with pytest.raises(RunValidationError):
        orchestrator.create_run(bad, ISSUE)


def test_create_run_rejects_oversized_issue(orchestrator: MentorOrchestrator) -> None:
    with pytest.raises(RunValidationError):
        orchestrator.create_run("C:/repo", "x" * 9000)


def test_rejected_creation_persists_nothing(orchestrator: MentorOrchestrator) -> None:
    with pytest.raises(RunValidationError):
        orchestrator.create_run("", ISSUE)
    assert orchestrator.list_runs() == []


# ── Deterministic advancement ─────────────────────────────────────────────────


def test_advance_moves_from_created_to_repository_context(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    result = orchestrator.advance(run.run_id)
    assert result.run.current_state is WorkflowState.REPOSITORY_CONTEXT
    assert result.run.status is RunStatus.PENDING
    assert result.stalled is False
    assert result.advanced is True


def test_advance_moves_from_repository_to_architecture_context(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.advance(run.run_id)
    result = orchestrator.advance(run.run_id)
    assert result.run.current_state is WorkflowState.ARCHITECTURE_CONTEXT
    assert result.stalled is False


def test_run_until_stalled_stops_at_the_phase9_approval_gate(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    """A run walks the real stages, then stalls at the Phase 9 boundary.

    Phase 7 made ``INVESTIGATION_PENDING`` real, Phase 8 made
    ``EVIDENCE_PENDING`` and ``ROOT_CAUSE_ANALYSIS`` real, and Phase 9 made
    ``ROOT_CAUSE_READY`` and ``FIX_PROPOSAL`` real, so the deterministic walk now
    continues to ``FIX_PROPOSAL_READY`` and stops there.  That is the Phase 9
    boundary: a fix has been *proposed*, and applying or verifying it is Phase
    10 and 11's job behind a human.
    """
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    result = orchestrator.run_until_stalled(run.run_id)
    assert result.stalled is True
    assert result.run.current_state is WorkflowState.FIX_PROPOSAL_READY
    assert result.run.status is RunStatus.PENDING


def test_an_approved_run_applies_the_fix_and_verifies_it(
    orchestrator: MentorOrchestrator, defective_repo: Path
) -> None:
    """The whole Phase 11 walk, driven the only way it can be driven.

    Phase 10's version of this test forced past the gate, which the state machine
    permitted because the gate was a placeholder.  Phase 11 makes the gate a wall
    with a door in it, so the test uses the door: a real approval, a real write, a
    real pytest run against the applied change.

    Every claim below is the reason the phase exists.  The run reaches
    ``COMPLETED``; the file on disk holds the fix; the only ``VERIFIED_RESULT``
    evidence comes from a runner that exited zero; and the diagnosis is repeated
    nowhere, because each phase re-derives from the run rather than from the phase
    before it.
    """
    run = orchestrator.create_run(str(defective_repo), ISSUE)
    result = orchestrator.run_until_stalled(run.run_id)
    assert result.run.current_state is WorkflowState.FIX_PROPOSAL_READY
    assert result.stalled is True

    final = orchestrator.approve_and_continue(
        run.run_id, approved_by="reviewer", comment="the arithmetic is wrong"
    )

    assert final.run.current_state is WorkflowState.COMPLETED
    assert final.run.status is RunStatus.COMPLETED
    assert final.stalled is False

    # The repository now holds the approved change, and nothing else moved.
    changed = defective_repo / "app" / "services" / "discount_service.py"
    assert FIXED_DISCOUNT in changed.read_text(encoding="utf-8")
    assert BUGGY_DISCOUNT not in changed.read_text(encoding="utf-8")

    # A verified result exists, and it came from an actual exit code.
    verification = load_verification(final.run)
    assert verification is not None
    assert verification.status is VerificationStatus.PASSED
    assert verification.tests_executed is True
    assert verification.regression_check is True
    assert verification.test_run.exit_code == 0
    assert verification.test_run.passed == 1
    # Phase 11 writes no test source, so the discriminating test here was
    # pre-existing in the repository rather than generated by the tool.
    assert verification.defect_demonstrated is True
    assert verification.limitations

    verified = [e for e in final.run.evidence if e.evidence_type.value == "VERIFIED_RESULT"]
    assert verified, "a passing runner must leave a typed result"
    assert all(e.agent == VERIFIER_AGENT for e in verified)

    types = [e.event_type for e in orchestrator.list_events(run.run_id)]
    assert types[-1] is WorkflowEventType.RUN_COMPLETED
    for expected in (
        WorkflowEventType.FIX_APPROVAL_REQUIRED,
        WorkflowEventType.FIX_APPROVED,
        WorkflowEventType.FIX_APPLICATION_STARTED,
        WorkflowEventType.FIX_APPLIED,
        WorkflowEventType.TEST_PLAN_STARTED,
        WorkflowEventType.VERIFICATION_STARTED,
        WorkflowEventType.VERIFICATION_COMPLETED,
    ):
        assert expected in types, f"{expected.value} is missing from the run"
    # The gate closes before it opens: nothing is written before the approval.
    assert types.index(WorkflowEventType.FIX_APPROVAL_REQUIRED) < types.index(
        WorkflowEventType.FIX_APPROVED
    )
    assert types.index(WorkflowEventType.FIX_APPROVED) < types.index(
        WorkflowEventType.FIX_APPLICATION_STARTED
    )


def test_a_suite_that_ignores_the_defect_demonstrates_nothing(
    orchestrator: MentorOrchestrator, blind_repo: Path
) -> None:
    """A green suite is not proof, and the run has to say so.

    ``blind_repo`` is the same defect as ``defective_repo`` with a test that
    divides evenly, so it is green before the fix and green after it.  The whole
    run therefore succeeds — ``COMPLETED``, exit code 0, no regression — and
    ``defect_demonstrated`` is still false.

    This is the test that would have caught the tempting implementation.  Deriving
    the flag from the plan's ``discriminating_condition`` passes the flattering
    test above and fails here; so does reading the test's source for something
    that looks like the changed arithmetic.  Only re-running the suite against the
    reverted change gives the right answer on both fixtures, because it asks the
    suite instead of guessing what the suite would say.
    """
    run = orchestrator.create_run(str(blind_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    final = orchestrator.approve_and_continue(
        run.run_id, approved_by="reviewer", comment="the arithmetic is wrong"
    )

    # Everything else about the run is a success.  Only the claim is not.
    assert final.run.current_state is WorkflowState.COMPLETED
    verification = load_verification(final.run)
    assert verification is not None
    assert verification.status is VerificationStatus.PASSED
    assert verification.tests_executed is True
    assert verification.regression_check is True
    assert verification.test_run.exit_code == 0

    assert verification.defect_demonstrated is False
    assert any(
        "also pass against the tree with the change reverted" in finding
        for finding in verification.findings
    ), "the refusal must say *why* it refused, not just that it did"


def test_the_counterfactual_does_not_touch_the_analysed_repository(
    orchestrator: MentorOrchestrator, defective_repo: Path
) -> None:
    """Deciding the flag must not write to the repository under analysis.

    The counterfactual reverts the change, which is the one thing this project
    must never do to somebody's working tree: the point of the scratch copy is
    that the pre-image can be reconstructed freely.  ``_snapshot`` is the same
    helper the repository-mutation tests above use, so this asserts against the
    convention rather than a bespoke walk.
    """
    before = _snapshot(defective_repo)
    run = orchestrator.create_run(str(defective_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    final = orchestrator.approve_and_continue(
        run.run_id, approved_by="reviewer", comment="the arithmetic is wrong"
    )
    verification = load_verification(final.run)
    assert verification is not None
    assert verification.defect_demonstrated is True

    after = _snapshot(defective_repo)
    # Only the one approved file may differ, and it differs by exactly the fix.
    assert set(before) == set(after)
    for path, digest in before.items():
        if "discount_service.py" in path:
            assert digest != after[path]
        else:
            assert after[path] == digest, f"{path} changed during verification"
    assert FIXED_DISCOUNT in (
        defective_repo / "app" / "services" / "discount_service.py"
    ).read_text(encoding="utf-8")
    assert BUGGY_DISCOUNT not in (
        defective_repo / "app" / "services" / "discount_service.py"
    ).read_text(encoding="utf-8")


def test_no_recorded_change_leaves_the_flag_undecided(defective_repo: Path) -> None:
    """A proposal with nothing to revert cannot produce a counterfactual."""
    empty = FixProposal(
        run_id="00000000-0000-4000-8000-000000000000",
        issue=ISSUE,
        status=FixProposalStatus.INSUFFICIENT_EVIDENCE,
        rationale="nothing to propose",
        changes=[],
    )
    discriminated, reason = counterfactual_discrimination(
        ControlledTestExecutor(),
        root=defective_repo,
        targets=["tests/test_discount.py"],
        proposal=empty,
        timeout_seconds=30.0,
    )
    assert discriminated is False
    assert "not decided" in reason


def test_an_undecidable_counterfactual_reports_false_not_a_guess(
    tmp_path: Path,
) -> None:
    """A pre-image that is not there yields "not decided", never a defect claim.

    The change is pointed at a line range the file does not contain.  That is
    exactly the situation the real pipeline refuses to write through, so the
    counterfactual refuses to answer through it too: guessing a pre-image would
    produce a confident "it fails without the fix" for a tree that never
    existed.
    """
    repo = tmp_path / "MismatchApp"
    (repo / "app").mkdir(parents=True)
    (repo / "app" / "mod.py").write_text("x = 1\n", encoding="utf-8")

    mismatched = FixProposal(
        run_id="00000000-0000-4000-8000-000000000000",
        issue=ISSUE,
        status=FixProposalStatus.READY_FOR_REVIEW,
        rationale="points at a line the file does not have",
        changes=[
            FixChange(
                file_path="app/mod.py",
                line=99,
                line_end=99,
                before="x = 0",
                after="x = 2",
            )
        ],
    )
    discriminated, reason = counterfactual_discrimination(
        ControlledTestExecutor(),
        root=repo,
        targets=["tests/test_mod.py"],
        proposal=mismatched,
        timeout_seconds=30.0,
    )
    assert discriminated is False
    assert "not decided" in reason
    # And the real tree is untouched by the failure.
    assert (repo / "app" / "mod.py").read_text(encoding="utf-8") == "x = 1\n"


def test_an_unapproved_run_never_writes_and_emits_its_own_waiting_state(
    orchestrator: MentorOrchestrator, defective_repo: Path
) -> None:
    """A run that nobody approved is a run whose file is untouched.

    This is the negative of the test above and it is the one that matters most: a
    system that can only demonstrate success by writing files is not a system with
    an approval gate.
    """
    before = _snapshot(defective_repo)
    run = orchestrator.create_run(str(defective_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)

    # Advancing repeatedly cannot get past the gate, however many times it is asked.
    for _ in range(5):
        result = orchestrator.advance(run.run_id)
        assert result.run.current_state is WorkflowState.FIX_PROPOSAL_READY
        assert result.stalled is True

    assert _snapshot(defective_repo) == before
    assert (defective_repo / "app" / "services" / "discount_service.py").read_text(
        encoding="utf-8"
    ).count(BUGGY_DISCOUNT) == 1

    # And it says why it is stopped, in its own vocabulary.
    pending = [
        e
        for e in orchestrator.list_events(run.run_id)
        if e.event_type is WorkflowEventType.FIX_APPROVAL_REQUIRED
    ]
    assert pending, "a run waiting for a human must say so"
    assert pending[-1].payload["awaiting_human_approval"] is True
    assert pending[-1].payload["changed_repository"] is False
    assert pending[-1].payload["approval_endpoint"].endswith("/approve-fix")


def test_the_phase9_proposal_is_recorded_without_being_applied(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    """Phase 9 output is a proposal in ``run.context``, never a repository edit."""
    from app.services.fix_proposal_stage import load_fix_proposal

    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    final = orchestrator.get_run(run.run_id)

    proposal = load_fix_proposal(final)
    assert proposal is not None, "Phase 9 must record an honest outcome"
    assert proposal.tests_executed is False
    if proposal.is_ready_for_review():
        assert proposal.unified_diff
    else:
        # ``sample_repo`` has no diagnosed defect, so an honest negative is the
        # expected result — and it must carry no diff.
        assert proposal.unified_diff is None
        assert proposal.changes == []


def test_advance_is_deterministic(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    states = []
    for _ in range(3):
        run = orchestrator.create_run(str(sample_repo), ISSUE)
        walk = orchestrator.run_until_stalled(run.run_id)
        states.append(walk.run.current_state)
    assert states == [WorkflowState.FIX_PROPOSAL_READY] * 3



def test_advance_reports_no_progress_on_terminal_run(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.cancel_run(run.run_id)
    before = orchestrator.event_count(run.run_id)
    result = orchestrator.advance(run.run_id)
    assert result.stalled is True
    assert result.events == []
    assert result.run.current_state is WorkflowState.CANCELLED
    assert orchestrator.event_count(run.run_id) == before


def test_missing_run_raises(orchestrator: MentorOrchestrator) -> None:
    missing = "00000000-0000-4000-8000-000000000000"
    with pytest.raises(RunNotFoundError):
        orchestrator.get_run(missing)
    with pytest.raises(RunNotFoundError):
        orchestrator.advance(missing)


# ── Failure transitions ───────────────────────────────────────────────────────


def test_unreadable_repository_fails_the_run(
    orchestrator: MentorOrchestrator, tmp_path: Path
) -> None:
    run = orchestrator.create_run(str(tmp_path / "does-not-exist"), ISSUE)
    result = orchestrator.run_until_stalled(run.run_id)
    assert result.run.current_state is WorkflowState.FAILED
    assert result.run.status is RunStatus.FAILED
    assert result.run.error is not None
    assert "does not exist" in result.run.error.lower()


def test_failure_emits_stage_failed_and_run_failed(
    orchestrator: MentorOrchestrator, tmp_path: Path
) -> None:
    run = orchestrator.create_run(str(tmp_path / "nope"), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    types = [e.event_type for e in orchestrator.list_events(run.run_id)]
    assert WorkflowEventType.STAGE_FAILED in types
    assert WorkflowEventType.RUN_FAILED in types
    assert types[-1] is WorkflowEventType.RUN_FAILED


def test_raising_stage_fails_the_run_safely(sample_repo: Path) -> None:
    class ExplodingStage:
        state = WorkflowState.REPOSITORY_CONTEXT

        def execute(self, context: StageContext) -> StageResult:
            raise RuntimeError("boom")

    orch = MentorOrchestrator(stages={ExplodingStage.state: ExplodingStage()})
    run = orch.create_run(str(sample_repo), ISSUE)
    result = orch.run_until_stalled(run.run_id)
    assert result.run.current_state is WorkflowState.FAILED
    assert "boom" in (result.run.error or "")


def test_stage_returning_failure_fails_the_run(sample_repo: Path) -> None:
    class FailingStage:
        state = WorkflowState.REPOSITORY_CONTEXT

        def execute(self, context: StageContext) -> StageResult:
            return StageResult.failed("nope", "deliberate failure")

    orch = MentorOrchestrator(stages={FailingStage.state: FailingStage()})
    run = orch.create_run(str(sample_repo), ISSUE)
    result = orch.run_until_stalled(run.run_id)
    assert result.run.current_state is WorkflowState.FAILED
    assert result.run.error == "deliberate failure"


def test_stage_returning_wrong_type_fails_the_run(sample_repo: Path) -> None:
    class BadStage:
        state = WorkflowState.REPOSITORY_CONTEXT

        def execute(self, context: StageContext):  # type: ignore[no-untyped-def]
            return "not a StageResult"

    orch = MentorOrchestrator(stages={BadStage.state: BadStage()})
    run = orch.create_run(str(sample_repo), ISSUE)
    result = orch.run_until_stalled(run.run_id)
    assert result.run.current_state is WorkflowState.FAILED
    assert "StageResult" in (result.run.error or "")


def test_stage_cannot_steer_the_workflow(sample_repo: Path) -> None:
    """A stage declaring an illegal next state is refused."""

    class RogueStage:
        state = WorkflowState.REPOSITORY_CONTEXT

        def execute(self, context: StageContext) -> StageResult:
            return StageResult.completed(
                "trying to skip ahead",
                next_state=WorkflowState.COMPLETED,
            )

    orch = MentorOrchestrator(stages={RogueStage.state: RogueStage()})
    run = orch.create_run(str(sample_repo), ISSUE)
    result = orch.run_until_stalled(run.run_id)
    assert result.run.current_state is WorkflowState.FAILED
    assert "only permits" in (result.run.error or "")


def test_missing_stage_handler_stalls_without_failing(sample_repo: Path) -> None:
    """An unwired state is a gap, not a workflow error."""
    orch = MentorOrchestrator(stages={})
    run = orch.create_run(str(sample_repo), ISSUE)
    result = orch.run_until_stalled(run.run_id)
    assert result.run.current_state is WorkflowState.CREATED
    assert result.run.status is RunStatus.PENDING
    assert result.run.error is None
    assert result.stalled is True


def test_failed_run_cannot_be_advanced(
    orchestrator: MentorOrchestrator, tmp_path: Path
) -> None:
    run = orchestrator.create_run(str(tmp_path / "nope"), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    after = orchestrator.event_count(run.run_id)
    result = orchestrator.advance(run.run_id)
    assert result.run.current_state is WorkflowState.FAILED
    assert result.events == []
    assert orchestrator.event_count(run.run_id) == after


def test_fail_run_helper(orchestrator: MentorOrchestrator, sample_repo: Path) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    failed = orchestrator.fail_run(run.run_id, "operator abort")
    assert failed.current_state is WorkflowState.FAILED
    assert failed.status is RunStatus.FAILED
    assert failed.error == "operator abort"


# ── Terminal state protection ─────────────────────────────────────────────────


def test_complete_run_requires_a_verified_result(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    """``CREATED`` is not a state you may complete from, for the same reason as ever."""
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    with pytest.raises(InvalidTransitionError):
        orchestrator.complete_run(run.run_id)


def test_complete_run_is_refused_before_a_real_test_run(
    orchestrator: MentorOrchestrator, defective_repo: Path
) -> None:
    """A run parked at the gate is not completable, and asking must not change it.

    This is the guarantee ``COMPLETED`` used to lack: the state is only reachable
    from ``VERIFIED``, which is only reachable from a recorded runner exit code.
    """
    run = orchestrator.create_run(str(defective_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)

    for _ in range(3):
        with pytest.raises(InvalidTransitionError):
            orchestrator.complete_run(run.run_id)

    assert orchestrator.get_run(run.run_id).current_state is (
        WorkflowState.FIX_PROPOSAL_READY
    )
    # And the failed attempts left no trace in the event log.
    types = [e.event_type for e in orchestrator.list_events(run.run_id)]
    assert WorkflowEventType.RUN_COMPLETED not in types


def test_complete_run_walks_the_full_path(
    orchestrator: MentorOrchestrator, defective_repo: Path
) -> None:
    run = orchestrator.create_run(str(defective_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    orchestrator.approve_fix(run.run_id, approved_by="reviewer")

    # Approval alone does not complete anything; the stages have to run.
    assert orchestrator.get_run(run.run_id).current_state is WorkflowState.FIX_APPROVED
    with pytest.raises(InvalidTransitionError):
        orchestrator.complete_run(run.run_id)

    advanced = orchestrator.run_until_stalled(run.run_id)
    assert advanced.run.current_state is WorkflowState.VERIFIED

    done = orchestrator.complete_run(run.run_id)
    assert done.current_state is WorkflowState.COMPLETED
    assert done.status is RunStatus.COMPLETED
    types = [e.event_type for e in orchestrator.list_events(run.run_id)]
    assert types[-1] is WorkflowEventType.RUN_COMPLETED


def test_advance_never_auto_completes_a_run(
    orchestrator: MentorOrchestrator, defective_repo: Path
) -> None:
    """Neither a placeholder nor a real stage may be enough to reach COMPLETED.

    Phase 11 made almost every stage real, so this test now proves something
    stronger than it did in Phase 6: a run driven purely by ``advance()`` still
    cannot complete, because ``COMPLETED`` is only reachable through
    ``complete_run()`` and that needs a verified result first.
    """
    run = orchestrator.create_run(str(defective_repo), ISSUE)
    for _ in range(20):
        result = orchestrator.advance(run.run_id)
        assert result.run.current_state is not WorkflowState.COMPLETED
    # Twenty advances from the top lands at the gate, and stays there.
    assert orchestrator.get_run(run.run_id).current_state is (
        WorkflowState.FIX_PROPOSAL_READY
    )

    # Approve, and drive it with advance() alone all the way to VERIFIED.
    orchestrator.approve_fix(run.run_id, approved_by="reviewer")
    for _ in range(20):
        result = orchestrator.advance(run.run_id)
        assert result.run.current_state is not WorkflowState.COMPLETED
        if result.run.current_state is WorkflowState.VERIFIED:
            break
    assert orchestrator.get_run(run.run_id).current_state is WorkflowState.VERIFIED


def test_completed_run_is_immutable(
    orchestrator: MentorOrchestrator, defective_repo: Path
) -> None:
    run = orchestrator.create_run(str(defective_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    orchestrator.approve_and_continue(run.run_id, approved_by="reviewer")
    assert orchestrator.get_run(run.run_id).current_state is WorkflowState.COMPLETED

    with pytest.raises(InvalidTransitionError):
        orchestrator.complete_run(run.run_id)
    with pytest.raises(InvalidTransitionError):
        orchestrator.cancel_run(run.run_id)
    with pytest.raises(InvalidTransitionError):
        orchestrator.fail_run(run.run_id, "too late")

    assert orchestrator.get_run(run.run_id).current_state is WorkflowState.COMPLETED


def test_cancel_run_from_any_non_terminal_state(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    cancelled = orchestrator.cancel_run(run.run_id)
    assert cancelled.current_state is WorkflowState.CANCELLED
    assert cancelled.status is RunStatus.CANCELLED
    types = [e.event_type for e in orchestrator.list_events(run.run_id)]
    assert types[-1] is WorkflowEventType.RUN_CANCELLED
    with pytest.raises(InvalidTransitionError):
        orchestrator.cancel_run(run.run_id)


# ── Event recording ───────────────────────────────────────────────────────────


def test_run_creation_emits_run_created(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    events = orchestrator.list_events(run.run_id)
    assert len(events) == 1
    assert events[0].event_type is WorkflowEventType.RUN_CREATED
    assert events[0].sequence == 1
    assert events[0].state is WorkflowState.CREATED
    assert events[0].status is RunStatus.PENDING


def test_full_walk_emits_events_in_order(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    events = orchestrator.list_events(run.run_id)
    assert [e.sequence for e in events] == list(range(1, len(events) + 1))
    assert [e.event_type for e in events] == [
        WorkflowEventType.RUN_CREATED,
        # Phase 3 — repository context.
        WorkflowEventType.STAGE_STARTED,
        WorkflowEventType.STAGE_COMPLETED,
        WorkflowEventType.STATE_CHANGED,
        # Phase 5 — architecture context.
        WorkflowEventType.STAGE_STARTED,
        WorkflowEventType.STAGE_COMPLETED,
        WorkflowEventType.STATE_CHANGED,
        # Phase 7 — parallel investigation.  Every investigator announces itself
        # before any completes, which is what makes the parallelism observable.
        WorkflowEventType.STAGE_STARTED,
        WorkflowEventType.INVESTIGATION_STARTED,
        WorkflowEventType.INVESTIGATOR_STARTED,
        WorkflowEventType.INVESTIGATOR_STARTED,
        WorkflowEventType.INVESTIGATOR_STARTED,
        WorkflowEventType.INVESTIGATOR_COMPLETED,
        WorkflowEventType.INVESTIGATOR_COMPLETED,
        WorkflowEventType.INVESTIGATOR_COMPLETED,
        WorkflowEventType.INVESTIGATION_COMPLETED,
            WorkflowEventType.STAGE_COMPLETED,
            WorkflowEventType.STATE_CHANGED,
            # Phase 8 - evidence synthesis.
            WorkflowEventType.STAGE_STARTED,
            WorkflowEventType.EVIDENCE_SYNTHESIS_STARTED,
            WorkflowEventType.EVIDENCE_SYNTHESIS_COMPLETED,
            WorkflowEventType.STAGE_COMPLETED,
            WorkflowEventType.STATE_CHANGED,
            # Phase 8 - root-cause analysis, then the Phase 9 hand-off and the
            # proposal itself, then the Phase 9 approval gate.
            WorkflowEventType.STAGE_STARTED,
            WorkflowEventType.ROOT_CAUSE_ANALYSIS_STARTED,
            WorkflowEventType.ROOT_CAUSE_ANALYSIS_COMPLETED,
            WorkflowEventType.STAGE_COMPLETED,
            WorkflowEventType.STATE_CHANGED,
            WorkflowEventType.STAGE_STARTED,
            WorkflowEventType.STAGE_COMPLETED,
            WorkflowEventType.STATE_CHANGED,
            WorkflowEventType.STAGE_STARTED,
            WorkflowEventType.FIX_PROPOSAL_STARTED,
            WorkflowEventType.FIX_PROPOSAL_COMPLETED,
            WorkflowEventType.STAGE_COMPLETED,
            WorkflowEventType.STATE_CHANGED,
            WorkflowEventType.STAGE_STARTED,
            WorkflowEventType.STAGE_PENDING,
            WorkflowEventType.STATE_CHANGED,
        ]


def test_investigator_start_events_precede_every_completion(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    """No investigator may report completion before all three have started.

    This is the observable signature of concurrent execution: if the runner
    were sequential, the third ``INVESTIGATOR_STARTED`` would come after the
    first ``INVESTIGATOR_COMPLETED``.
    """
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    types = [e.event_type for e in orchestrator.list_events(run.run_id)]
    last_start = len(types) - 1 - types[::-1].index(
        WorkflowEventType.INVESTIGATOR_STARTED
    )
    first_done = types.index(WorkflowEventType.INVESTIGATOR_COMPLETED)
    assert last_start < first_done
    assert types.count(WorkflowEventType.INVESTIGATOR_STARTED) == 3
    assert types.count(WorkflowEventType.INVESTIGATOR_COMPLETED) == 3


def test_state_changed_events_record_the_path(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    path = [
        (e.payload["from_state"], e.payload["to_state"])
        for e in orchestrator.list_events(run.run_id)
        if e.event_type is WorkflowEventType.STATE_CHANGED
    ]
    assert path == [
        ("CREATED", "REPOSITORY_CONTEXT"),
        ("REPOSITORY_CONTEXT", "ARCHITECTURE_CONTEXT"),
        ("ARCHITECTURE_CONTEXT", "INVESTIGATION_PENDING"),
        ("INVESTIGATION_PENDING", "EVIDENCE_PENDING"),
        ("EVIDENCE_PENDING", "ROOT_CAUSE_ANALYSIS"),
        ("ROOT_CAUSE_ANALYSIS", "ROOT_CAUSE_READY"),
        ("ROOT_CAUSE_READY", "FIX_PROPOSAL"),
        ("FIX_PROPOSAL", "FIX_PROPOSAL_READY"),
    ]


def test_advance_returns_the_events_it_emitted(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    result = orchestrator.advance(run.run_id)
    assert [e.event_type for e in result.events] == [
        WorkflowEventType.STAGE_STARTED,
        WorkflowEventType.STAGE_COMPLETED,
        WorkflowEventType.STATE_CHANGED,
    ]
    assert orchestrator.event_count(run.run_id) == 4  # 1 from creation + 3


def test_stage_status_is_running_only_during_execution(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    events = orchestrator.list_events(run.run_id)
    started = [e for e in events if e.event_type is WorkflowEventType.STAGE_STARTED]
    assert all(e.status is RunStatus.RUNNING for e in started)
    assert orchestrator.get_run(run.run_id).status is RunStatus.PENDING


def test_list_events_rejects_unknown_runs(orchestrator: MentorOrchestrator) -> None:
    with pytest.raises(RunNotFoundError):
        orchestrator.list_events("00000000-0000-4000-8000-000000000000")


# ── Honesty: no fabricated results ────────────────────────────────────────────


def test_placeholder_stages_produce_no_evidence(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    """Stages past the implemented ones must never contribute evidence.

    Phases 7 and 8 add three more evidence-producing stages to the Phase 3/5 pair.
    Phase 8 contributes none of its own: it only reads Phase 7 evidence and
    records an analysis, so the agent set is unchanged by Phase 8.  The
    placeholders at ``ROOT_CAUSE_READY`` and beyond stay silent.
    """
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    for _ in range(8):
        orchestrator.advance(run.run_id)
    final = orchestrator.get_run(run.run_id)
    # Only the implemented stages contributed evidence.
    assert {e.agent for e in final.evidence} == {
        "repository_context",
        "architecture_context",
        "code_investigator",
        "test_investigator",
        "dependency_investigator",
    }


def test_no_evidence_type_is_upgraded_beyond_what_was_observed(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    """Phase 7 may speculate, but only as a labelled HYPOTHESIS.

    Phases 1–6 had no reason to emit anything but observations, so the original
    assertion (``types <= {OBSERVATION}``) was correct then.  Phase 7 is allowed
    to produce ``HYPOTHESIS`` and investigative ``PROPOSED_ACTION`` — that is the
    deliverable — but the invariant that must survive is that nothing is ever
    promoted to ``VERIFIED_RESULT`` without a test run, and no unlabelled
    speculation escapes.  ``VERIFIED_RESULT`` is unavailable to Phase 7 at all.
    """
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    evidence = orchestrator.get_run(run.run_id).evidence
    types = {e.evidence_type for e in evidence}

    assert EvidenceType.VERIFIED_RESULT not in types
    # The context stages still only ever observe.
    context_agents = {"repository_context", "architecture_context"}
    assert {e.evidence_type for e in evidence if e.agent in context_agents} == {
        EvidenceType.OBSERVATION
    }
    # Every speculative item is typed as such, never as a fact.
    for item in evidence:
        if item.agent in context_agents:
            continue
        if item.metadata.get("conclusion"):
            assert item.evidence_type is EvidenceType.HYPOTHESIS
        assert 0.0 < (item.confidence or 0.0) <= 1.0 or (
            item.evidence_type is not EvidenceType.HYPOTHESIS
        )
        if item.evidence_type is EvidenceType.HYPOTHESIS:
            assert item.confidence is not None


def test_no_root_cause_is_claimed(sample_repo: Path) -> None:
    run = MentorOrchestrator().create_run(str(sample_repo), ISSUE)
    orchestrator = MentorOrchestrator()
    orchestrator.run_until_stalled(run.run_id)
    final = orchestrator.get_run(run.run_id)
    serialized = " ".join(
        e.title + " " + e.description for e in final.evidence
    ).lower()
    for forbidden in ("root cause", "confirmed bug", "verified"):
        assert forbidden not in serialized


def test_placeholder_reports_itself_as_unimplemented(sample_repo: Path) -> None:
    stage = PlaceholderStage(
        WorkflowState.INVESTIGATION_PENDING, 7, "not built yet"
    )
    result = stage.execute(StageContext(run=MentorRun(
        run_id="11111111-1111-4111-8111-111111111111",
        repository_path="C:/repo",
        issue=ISSUE,
    )))
    assert result.outcome is StageOutcome.PENDING
    assert result.next_state is None
    assert result.evidence == []
    assert result.data["implemented"] is False
    assert result.data["target_phase"] == 7
    assert "not implemented" in result.message.lower()


def test_evidence_is_typed_and_observable(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    """Every evidence item is a real, attributed, non-empty record.

    The per-item type may now legitimately differ by stage, so the assertion is
    on the shared contract — correct type enum, owning run, non-empty text —
    rather than on a single hard-coded type.
    """
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    for item in orchestrator.get_run(run.run_id).evidence:
        assert isinstance(item, Evidence)
        assert isinstance(item.evidence_type, EvidenceType)
        assert item.session_id == run.run_id
        assert item.title and item.description
        if item.evidence_type is EvidenceType.OBSERVATION:
            # An observation without a source is an untraceable claim.
            assert item.source_file or item.metadata.get("repository_level")
        if item.agent in INVESTIGATOR_AGENTS:
            # Phase 7 evidence must stay relative to the repository root.  Phase
            # 3 legitimately anchors a repository-level observation at the
            # absolute root, but an investigator citing a path must not leak the
            # host's directory layout or a traversable path.
            if item.source_file is not None:
                assert not Path(item.source_file).is_absolute()
                assert ".." not in Path(item.source_file).parts


def test_context_records_observed_counts(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    result = orchestrator.run_until_stalled(run.run_id)
    assert result.run.context["repository_name"] == "SampleApp"
    assert result.run.context["total_files"] >= 4
    assert result.run.context["project_type"] == "web_api"
    assert result.run.context["route_count"] >= 1


# ── No repository modification ────────────────────────────────────────────────


def test_full_workflow_does_not_modify_the_repository(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    before = _snapshot(sample_repo)
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    for _ in range(8):
        orchestrator.advance(run.run_id)
    assert _snapshot(sample_repo) == before


def test_failed_workflow_does_not_modify_the_repository(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    before = _snapshot(sample_repo)
    orchestrator.fail_run(
        orchestrator.create_run(str(sample_repo), ISSUE).run_id, "abort"
    )
    assert _snapshot(sample_repo) == before


def test_secret_files_are_not_read_into_evidence(
    orchestrator: MentorOrchestrator, tmp_path: Path
) -> None:
    repo = tmp_path / "SecretRepo"
    repo.mkdir()
    (repo / "main.py").write_text("x = 1\n", encoding="utf-8")
    (repo / ".env").write_text("API_KEY=super-secret-value\n", encoding="utf-8")

    run = orchestrator.create_run(str(repo), ISSUE)
    result = orchestrator.run_until_stalled(run.run_id)
    blob = " ".join(
        [result.run.issue, result.run.repository_path]
        + [e.title + e.description for e in result.run.evidence]
    )
    assert "super-secret-value" not in blob


# ── Multiple independent runs ─────────────────────────────────────────────────


def test_runs_are_independent(orchestrator: MentorOrchestrator, sample_repo: Path) -> None:
    first = orchestrator.create_run(str(sample_repo), ISSUE)
    second = orchestrator.create_run(str(sample_repo), "A different question")

    orchestrator.run_until_stalled(first.run_id)
    assert orchestrator.get_run(second.run_id).current_state is WorkflowState.CREATED

    events_a = orchestrator.list_events(first.run_id)
    events_b = orchestrator.list_events(second.run_id)
    assert len(events_b) == 1
    assert all(e.run_id == first.run_id for e in events_a)
    assert all(e.run_id == second.run_id for e in events_b)


def test_interleaved_runs_do_not_contaminate(
    orchestrator: MentorOrchestrator, sample_repo: Path, tmp_path: Path
) -> None:
    good = orchestrator.create_run(str(sample_repo), ISSUE)
    bad = orchestrator.create_run(str(tmp_path / "missing"), ISSUE)

    orchestrator.advance(good.run_id)
    orchestrator.advance(bad.run_id)
    orchestrator.advance(good.run_id)
    orchestrator.advance(bad.run_id)

    assert orchestrator.get_run(good.run_id).current_state is (
        WorkflowState.ARCHITECTURE_CONTEXT
    )
    assert orchestrator.get_run(bad.run_id).current_state is WorkflowState.FAILED
    assert all(
        e.state is not WorkflowState.FAILED
        for e in orchestrator.list_events(good.run_id)
    )


def test_list_runs_returns_all(orchestrator: MentorOrchestrator, sample_repo: Path) -> None:
    ids = {
        orchestrator.create_run(str(sample_repo), ISSUE).run_id for _ in range(5)
    }
    assert {r.run_id for r in orchestrator.list_runs()} == ids


# ── Injection points for later phases ─────────────────────────────────────────


def test_default_registry_covers_every_drivable_state() -> None:
    """Every state on the forward path owns a stage, except the four that must not.

    ``CREATED`` has no stage because the first advance *targets*
    ``REPOSITORY_CONTEXT``.  The other three absences are the point of Phase 11:
    a stage for ``FIX_APPROVED`` would be a stage nobody authorised, one for
    ``VERIFIED`` would be a stage inventing its own verdict, and one for
    ``COMPLETED`` would mean the workflow could finish itself.
    """
    registry = build_default_stages()
    expected = {
        WorkflowState.REPOSITORY_CONTEXT,
        WorkflowState.ARCHITECTURE_CONTEXT,
        WorkflowState.INVESTIGATION_PENDING,
        WorkflowState.EVIDENCE_PENDING,
        WorkflowState.ROOT_CAUSE_ANALYSIS,
        WorkflowState.ROOT_CAUSE_READY,
        WorkflowState.FIX_PROPOSAL,
        WorkflowState.FIX_PROPOSAL_READY,
        WorkflowState.FIX_APPLYING,
        WorkflowState.FIX_APPLIED,
        WorkflowState.TEST_PLAN,
        WorkflowState.VERIFICATION,
    }
    assert set(registry) == expected
    for absent in (
        WorkflowState.CREATED,
        WorkflowState.FIX_APPROVED,
        WorkflowState.VERIFIED,
        WorkflowState.COMPLETED,
    ):
        assert absent not in registry, (
            f"{absent.value} must not own a stage: no stage may cross the approval "
            f"gate, assert its own verdict, or finish the run."
        )
    for state, handler in registry.items():
        assert isinstance(handler, StageHandler)
        assert handler.state is state


def test_registry_can_be_replaced(sample_repo: Path) -> None:
    calls: list[WorkflowState] = []

    class RecordingStage:
        def __init__(self, state: WorkflowState) -> None:
            self.state = state

        def execute(self, context: StageContext) -> StageResult:
            calls.append(self.state)
            return StageResult.completed("ok", next_state=self.state)

    registry = {
        s: RecordingStage(s)
        for s in (
            WorkflowState.REPOSITORY_CONTEXT,
            WorkflowState.ARCHITECTURE_CONTEXT,
            WorkflowState.INVESTIGATION_PENDING,
        )
    }
    orch = MentorOrchestrator(stages=registry)
    run = orch.create_run(str(sample_repo), ISSUE)
    orch.run_until_stalled(run.run_id)
    assert calls == [
        WorkflowState.REPOSITORY_CONTEXT,
        WorkflowState.ARCHITECTURE_CONTEXT,
        WorkflowState.INVESTIGATION_PENDING,
    ]


def test_registered_states_are_reported(orchestrator: MentorOrchestrator) -> None:
    assert WorkflowState.REPOSITORY_CONTEXT in orchestrator.registered_states()
    assert WorkflowState.INVESTIGATION_PENDING in orchestrator.registered_states()


def test_orchestrator_works_with_injected_stores(sample_repo: Path) -> None:
    from app.services.event_store import EventStore

    orch = MentorOrchestrator(run_store=RunStore(), event_store=EventStore())
    run = orch.create_run(str(sample_repo), ISSUE)
    result = orch.run_until_stalled(run.run_id)
    assert result.run.current_state is WorkflowState.FIX_PROPOSAL_READY
    assert orch.event_count(run.run_id) == len(orch.list_events(run.run_id))


# -- Phase 7 investigation stage --


def test_investigation_stage_stores_a_summary_on_the_run(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    final = orchestrator.get_run(run.run_id)
    summary = final.context["investigation_summary"]
    assert summary["status"] in ("COMPLETED", "PARTIAL")
    assert [i["investigator"] for i in summary["investigators"]] == [
        "code_investigator",
        "test_investigator",
        "dependency_investigator",
    ]


def test_investigation_stage_does_not_duplicate_evidence(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    """Evidence lives on the run; the summary must be a pointer, not a copy."""
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    final = orchestrator.get_run(run.run_id)
    investigator_evidence = [
        e for e in final.evidence if e.agent in INVESTIGATOR_AGENTS
    ]
    assert investigator_evidence
    # The stored summary carries no embedded evidence payloads.
    assert "evidence" not in final.context["investigation_summary"]


def test_investigation_stage_does_not_re_run_on_a_second_drive(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    """Re-driving a run parked past Phase 7 must not redo the investigation.

    Driving the run again re-emits the placeholder's "still pending" events, which
    is existing behaviour, and Phase 10's planner may run if the drive gets that
    far.  Neither is a re-investigation: the investigation itself is a completed
    stage, so it must not re-execute, re-append evidence, or announce its
    investigators a second time.  The count asserted is therefore the
    *investigator's* evidence, not the run's total — a total would turn this into
    an assertion about every later phase, which is a different test and a
    brittle one.
    """
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    before = orchestrator.get_run(run.run_id)
    evidence_before = len([e for e in before.evidence if e.agent in INVESTIGATOR_AGENTS])
    events_before = [
        e.event_type
        for e in orchestrator.list_events(run.run_id)
        if e.event_type
        in (
            WorkflowEventType.INVESTIGATION_STARTED,
            WorkflowEventType.INVESTIGATOR_STARTED,
            WorkflowEventType.INVESTIGATOR_COMPLETED,
            WorkflowEventType.INVESTIGATION_COMPLETED,
        )
    ]
    assert events_before

    orchestrator.run_until_stalled(run.run_id)
    after = orchestrator.get_run(run.run_id)
    assert len([e for e in after.evidence if e.agent in INVESTIGATOR_AGENTS]) == (
        evidence_before
    )
    events_after = [
        e.event_type
        for e in orchestrator.list_events(run.run_id)
        if e.event_type
        in (
            WorkflowEventType.INVESTIGATION_STARTED,
            WorkflowEventType.INVESTIGATOR_STARTED,
            WorkflowEventType.INVESTIGATOR_COMPLETED,
            WorkflowEventType.INVESTIGATION_COMPLETED,
        )
    ]
    assert events_after == events_before


def test_investigation_events_carry_their_evidence_counts(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    completions = [
        e
        for e in orchestrator.list_events(run.run_id)
        if e.event_type is WorkflowEventType.INVESTIGATOR_COMPLETED
    ]
    assert len(completions) == 3
    for event in completions:
        assert event.payload["evidence_count"] > 0
        assert event.payload["investigator"] in INVESTIGATOR_AGENTS


def test_investigation_events_never_imply_a_conclusion_was_reached(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    """The event stream must state that no root cause has been established.

    A user watching the SSE stream sees these messages alone, so the disclaimer
    has to be in the stream rather than buried in a report.
    """
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    orchestrator.run_until_stalled(run.run_id)
    events = orchestrator.list_events(run.run_id)
    completed = [
        e
        for e in events
        if e.event_type is WorkflowEventType.INVESTIGATION_COMPLETED
    ]
    assert len(completed) == 1
    assert "no root cause" in completed[0].message.lower()
    # Nothing in the investigation's own event stream asserts a cause or a
    # verification.  (The disclaimer itself contains the phrase "root cause", so
    # the check is for a *claim*, not for the words.)
    investigation_events = [
        e
        for e in events
        if e.event_type
        in (
            WorkflowEventType.INVESTIGATION_STARTED,
            WorkflowEventType.INVESTIGATOR_STARTED,
            WorkflowEventType.INVESTIGATOR_COMPLETED,
            WorkflowEventType.INVESTIGATOR_FAILED,
            WorkflowEventType.INVESTIGATION_COMPLETED,
        )
    ]
    for event in investigation_events:
        lowered = event.message.lower()
        for claim in ("root cause:", "confirmed bug", "verified fix", "tests pass"):
            assert claim not in lowered, (
                f"{event.event_type.value} claims a conclusion: {event.message!r}"
            )


def test_investigation_finds_something_for_a_related_issue(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    run = orchestrator.create_run(str(sample_repo), ISSUE)
    result = orchestrator.run_until_stalled(run.run_id)
    summary = result.run.context["investigation_summary"]
    assert summary["evidence_count"] == sum(
        i["evidence_count"] for i in summary["investigators"]
    )


def test_investigation_is_reproducible_across_runs(
    orchestrator: MentorOrchestrator, sample_repo: Path
) -> None:
    """Same repository, same issue ⇒ same findings, run after run.

    This is the determinism guarantee the whole investigation layer rests on:
    the UI will show two runs of the same question and the difference between
    them must be nothing.
    """
    titles = []
    for _ in range(2):
        run = orchestrator.create_run(str(sample_repo), ISSUE)
        orchestrator.run_until_stalled(run.run_id)
        titles.append(
            [
                (e.agent, e.title)
                for e in orchestrator.get_run(run.run_id).evidence
                if e.agent in INVESTIGATOR_AGENTS
            ]
        )
    assert titles[0] == titles[1]
