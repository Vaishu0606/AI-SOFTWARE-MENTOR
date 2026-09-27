"""
Phase 9 tests — the fix-proposal stages and the workflow hand-off.

Covers the stage layer rather than the engine (which ``test_fix_proposer``
owns): the ``ROOT_CAUSE_READY`` gate, proposal persistence and retrieval, event
emission, stage-to-state wiring, and the guarantee that a run stops at the human
approval gate without writing to the analysed repository.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.models.fix_proposal import FixProposal, FixProposalStatus
from app.models.root_cause import (
    ConfidenceBreakdown,
    ConfidenceLevel,
    RootCauseAnalysis,
    RootCauseKind,
    RootCauseStatus,
    SourceAnchor,
)
from app.models.workflow import (
    MentorRun,
    RunStatus,
    StageOutcome,
    StageResult,
    WorkflowEventType,
    WorkflowState,
)
from app.services.fix_proposal_stage import (
    FIX_PROPOSAL_CREATED_AT_KEY,
    FIX_PROPOSAL_KEY,
    FixProposalStage,
    RootCauseReadyStage,
    load_fix_proposal,
)
from app.services.orchestrator import MentorOrchestrator
from app.services.root_cause_stage import ROOT_CAUSE_KEY
from app.services.stages import StageContext, build_default_stages
from app.services.state_machine import (
    ALLOWED_TRANSITIONS,
    LINEAR_PROGRESSION,
    WorkflowStateMachine,
)

BUGGY = "    return int(subtotal_cents / 100) * code.value"
FIXED = "    return int(subtotal_cents * code.value / 100)"
ISSUE = "Checkout is failing when a discount code is applied."
RUN_ID = "11111111-1111-4111-8111-111111111111"


# ── Fixtures / builders ───────────────────────────────────────────────────────


def bug_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ShopDemo"
    (repo / "app" / "services").mkdir(parents=True, exist_ok=True)
    (repo / "app" / "services" / "discount_service.py").write_text(
        "def calculate_discount_cents(subtotal_cents, code):\n" + BUGGY + "\n",
        encoding="utf-8",
    )
    return repo


def stored_analysis(
    run_id: str = RUN_ID, *, line: int = 2, excerpt: str = BUGGY.strip()
) -> RootCauseAnalysis:
    """A Phase 8 analysis exactly as the real stage persists it."""
    return RootCauseAnalysis(
        run_id=run_id,
        issue=ISSUE,
        status=RootCauseStatus.DETERMINED,
        root_cause="The value is truncated before it is scaled.",
        root_cause_kind=RootCauseKind.TRUNCATION_BEFORE_SCALING,
        anchor=SourceAnchor(
            file_path="app/services/discount_service.py",
            line=line,
            function="calculate_discount_cents",
            excerpt=excerpt,
        ),
        confidence=0.48,
        confidence_level=ConfidenceLevel.MEDIUM,
        confidence_rationale="One direct observation, no corroboration.",
        confidence_breakdown=ConfidenceBreakdown(
            base=0.6,
            corroboration_bonus=0.0,
            score=0.48,
            level=ConfidenceLevel.MEDIUM,
            rationale="test fixture",
        ),
        supporting_evidence=[],
        tests_executed=False,
        verified_result_count=0,
    )


def make_run(
    repository_path: str,
    *,
    run_id: str = RUN_ID,
    state: WorkflowState = WorkflowState.FIX_PROPOSAL,
    with_analysis: bool = True,
    context: dict | None = None,
) -> MentorRun:
    stored = dict(context or {})
    if with_analysis:
        stored[ROOT_CAUSE_KEY] = stored_analysis(run_id).to_summary()
    return MentorRun(
        run_id=run_id,
        repository_path=repository_path,
        issue=ISSUE,
        current_state=state,
        status=RunStatus.PENDING,
        context=stored,
    )


def make_context(run: MentorRun, events: list | None = None) -> StageContext:
    def emit(event_type: str, message: str, payload: dict) -> None:
        if events is not None:
            events.append((event_type, message, payload))

    return StageContext(run=run, evidence=[], data={}, emit=emit)


# ── StageResult contracts ─────────────────────────────────────────────────────


def test_stage_result_succeeds_without_steering_the_workflow(tmp_path: Path) -> None:
    """A stage may only request its own state; the orchestrator rejects a move."""
    repo = bug_repo(tmp_path)
    result = FixProposalStage().execute(make_context(make_run(str(repo))))
    assert result.outcome is StageOutcome.COMPLETED
    assert result.next_state is WorkflowState.FIX_PROPOSAL
    assert result.error is None
    # Phase 9 contributes no evidence of its own: it consumes Phase 8's.
    assert result.evidence == []


def test_the_dynamic_data_kwargs_reach_stage_result_data(tmp_path: Path) -> None:
    """Guards the ``**{...}`` call the stage uses to publish context updates."""
    repo = bug_repo(tmp_path)
    result = FixProposalStage().execute(make_context(make_run(str(repo))))
    data = result.data
    assert FIX_PROPOSAL_KEY in data
    assert FIX_PROPOSAL_CREATED_AT_KEY in data
    assert data["fix_proposal_status"] == FixProposalStatus.READY_FOR_REVIEW.value
    assert data["fix_proposal_change_count"] == 1
    assert data["fix_proposal_tests_executed"] is False


def test_stage_result_failed_takes_a_single_error_string() -> None:
    """``StageResult.failed`` is typed ``error: str``.

    The gate once passed a tuple, which pydantic rejects at runtime — only the
    gate's *error* path exercised it, so it would have surfaced as a 500 on a
    wiring fault rather than the intended clean failure.
    """
    result = StageResult.failed("message", "one reason")
    assert result.error == "one reason"
    assert result.next_state is None
    assert result.outcome is StageOutcome.FAILED


# ── The ROOT_CAUSE_READY gate ─────────────────────────────────────────────────


def test_root_cause_gate_fails_when_no_analysis_is_recorded(tmp_path: Path) -> None:
    """A wiring fault must not be reported as an opinion about the repository."""
    repo = bug_repo(tmp_path)
    context = make_context(
        make_run(str(repo), state=WorkflowState.ROOT_CAUSE_READY, with_analysis=False)
    )
    result = RootCauseReadyStage().execute(context)
    assert result.outcome is StageOutcome.FAILED
    assert result.next_state is None
    assert "no root-cause analysis" in result.message
    assert "ROOT_CAUSE_ANALYSIS" in result.error


def test_root_cause_gate_passes_with_a_recorded_analysis(tmp_path: Path) -> None:
    """The gate is a precondition, not a substitute for the proposal stage."""
    context = make_context(
        make_run(str(bug_repo(tmp_path)), state=WorkflowState.ROOT_CAUSE_READY)
    )
    result = RootCauseReadyStage().execute(context)
    assert result.outcome is StageOutcome.COMPLETED
    assert result.next_state is WorkflowState.ROOT_CAUSE_READY
    assert result.data["root_cause_determined"] is True
    assert result.data["root_cause_handoff_status"] == RootCauseStatus.DETERMINED.value


def test_root_cause_gate_accepts_an_undetermined_analysis_without_failing(
    tmp_path: Path,
) -> None:
    """An honest "no root cause" must reach the approval gate, not fail the run.

    The gate asserts the analysis is *present*, not that it is positive: the
    developer still has to be shown the qualified conclusion, and Phase 9's
    ``INSUFFICIENT_EVIDENCE`` proposal is how that is reported.
    """
    run = make_run(str(bug_repo(tmp_path)), state=WorkflowState.ROOT_CAUSE_READY)
    analysis = stored_analysis()
    analysis.status = RootCauseStatus.INSUFFICIENT_EVIDENCE
    analysis.root_cause = None
    run.context[ROOT_CAUSE_KEY] = analysis.to_summary()
    result = RootCauseReadyStage().execute(make_context(run))
    assert result.outcome is StageOutcome.COMPLETED
    assert result.data["root_cause_determined"] is False


# ── Proposal persistence ──────────────────────────────────────────────────────


def test_stage_persists_a_reloadable_proposal(tmp_path: Path) -> None:
    repo = bug_repo(tmp_path)
    result = FixProposalStage().execute(make_context(make_run(str(repo))))
    restored = FixProposal.from_summary(
        result.data[FIX_PROPOSAL_KEY]["run_id"], result.data[FIX_PROPOSAL_KEY]
    )
    assert restored.status is FixProposalStatus.READY_FOR_REVIEW
    assert restored.changes[0].after.strip() == FIXED.strip()
    assert FIXED.strip() in restored.unified_diff
    # The wall clock lives beside the model, not inside it, so replay is exact.
    assert "created_at" not in restored.model_dump()
    assert result.data[FIX_PROPOSAL_CREATED_AT_KEY]


def test_proposal_is_byte_identical_when_reloaded(tmp_path: Path) -> None:
    repo = bug_repo(tmp_path)
    result = FixProposalStage().execute(make_context(make_run(str(repo))))
    summary = result.data[FIX_PROPOSAL_KEY]
    run_id = summary["run_id"]
    first = FixProposal.from_summary(run_id, summary)
    second = FixProposal.from_summary(run_id, summary)
    assert first.model_dump() == second.model_dump()


def test_load_fix_proposal_returns_none_when_absent(tmp_path: Path) -> None:
    run = make_run(str(bug_repo(tmp_path)), state=WorkflowState.ROOT_CAUSE_READY)
    assert load_fix_proposal(run) is None


def test_load_fix_proposal_survives_a_corrupt_summary(tmp_path: Path) -> None:
    """A bad summary reads as absent, so a read-only endpoint never 500s."""
    repo = bug_repo(tmp_path)
    assert load_fix_proposal(
        make_run(str(repo), context={FIX_PROPOSAL_KEY: {"garbage": True}})
    ) is None
    assert load_fix_proposal(
        make_run(str(repo), context={FIX_PROPOSAL_KEY: 7})
    ) is None
    assert load_fix_proposal(
        make_run(str(repo), context={FIX_PROPOSAL_KEY: "not a dict"})
    ) is None


# ── Events ────────────────────────────────────────────────────────────────────


def test_stage_emits_started_and_completed(tmp_path: Path) -> None:
    repo = bug_repo(tmp_path)
    events: list = []
    FixProposalStage().execute(make_context(make_run(str(repo)), events=events))
    kinds = {event[0] for event in events}
    assert WorkflowEventType.FIX_PROPOSAL_STARTED.value in kinds
    assert WorkflowEventType.FIX_PROPOSAL_COMPLETED.value in kinds
    assert WorkflowEventType.FIX_PROPOSAL_FAILED.value not in kinds


def test_completed_event_payload_admits_nothing_was_applied_or_run(tmp_path: Path) -> None:
    repo = bug_repo(tmp_path)
    events: list = []
    FixProposalStage().execute(make_context(make_run(str(repo)), events=events))
    completed = [
        e for e in events if e[0] == WorkflowEventType.FIX_PROPOSAL_COMPLETED.value
    ]
    assert len(completed) == 1
    payload = completed[0][2]
    assert payload["applied"] is False
    assert payload["tests_executed"] is False
    assert payload["change_count"] == 1
    assert payload["affected_files"] == ["app/services/discount_service.py"]


def test_stage_emits_failed_when_no_analysis_is_recorded(tmp_path: Path) -> None:
    events: list = []
    result = FixProposalStage().execute(
        make_context(
            make_run(str(bug_repo(tmp_path)), with_analysis=False), events=events
        )
    )
    assert result.outcome is StageOutcome.FAILED
    kinds = {event[0] for event in events}
    assert WorkflowEventType.FIX_PROPOSAL_FAILED.value in kinds
    assert WorkflowEventType.FIX_PROPOSAL_COMPLETED.value not in kinds


def test_stage_fails_loudly_on_an_unreadable_repository(tmp_path: Path) -> None:
    """An unexpected error is reported, never converted into "no fix needed"."""
    events: list = []
    result = FixProposalStage().execute(
        make_context(make_run(str(tmp_path / "missing")), events=events)
    )
    assert result.outcome is StageOutcome.FAILED
    assert result.error
    assert {event[0] for event in events} == {
        WorkflowEventType.FIX_PROPOSAL_STARTED.value,
        WorkflowEventType.FIX_PROPOSAL_FAILED.value,
    }


# ── The repository is never written to ────────────────────────────────────────


def test_running_the_stage_does_not_touch_the_repository(tmp_path: Path) -> None:
    repo = bug_repo(tmp_path)
    before = {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()}
    FixProposalStage().execute(make_context(make_run(str(repo))))
    after = {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()}
    assert after == before
    # The defect is still there: the diff is a proposal, not a repair.
    assert BUGGY in (repo / "app" / "services" / "discount_service.py").read_text(
        encoding="utf-8"
    )


#: Modules introduced by Phase 9.  Held to the same controls as the Phase 6 and
#: Phase 8 modules, and to one more: Phase 9 is the first phase allowed to *read*
#: a repository file, so it must still not be able to write or execute one.
PHASE9_MODULES = [
    "app/models/fix_proposal.py",
    "app/analysis/fix_proposer.py",
    "app/services/fix_proposal_stage.py",
]

#: Bare-name calls that would execute code, matching ``test_phase6_security``.
#: Kept as names rather than attribute names so ``re.compile`` is not a false
#: positive while a bare ``compile(...)`` still is caught.
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}

#: Attribute calls that would mutate a file or spawn a process.
FORBIDDEN_ATTRS = {
    "write_text", "write_bytes", "writelines", "unlink", "rmdir", "remove",
    "rename", "replace", "truncate", "mkdir", "touch", "rmtree", "copy",
    "copy2", "copyfile", "copytree", "move", "chmod", "symlink_to",
    "system", "popen", "spawn", "spawnl", "fork", "Popen", "check_output",
    "check_call", "execv", "execl", "execve", "run", "call",
}


def _called_names(tree: ast.AST) -> set[str]:
    """Every function name invoked in ``tree``, bare and attribute alike."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_no_phase9_module_calls_bare_execution_primitives() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    for relative in PHASE9_MODULES:
        tree = ast.parse((backend_root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLS, (
                    f"{relative} calls {node.func.id!r} at line {node.lineno}"
                )


def test_no_phase9_module_can_write_or_execute_anything() -> None:
    """Static guard, mirroring the Phase 6 security test.

    Phase 9 reads source and builds a diff.  A write or execution call appearing
    in these modules would mean the human-approval gate had been bypassed at the
    source level, before any API or UI could be consulted.
    """
    backend_root = Path(__file__).resolve().parents[1]
    for relative in PHASE9_MODULES:
        tree = ast.parse((backend_root / relative).read_text(encoding="utf-8"))
        called = _called_names(tree)
        offending = called & FORBIDDEN_ATTRS
        assert not offending, (
            f"{relative} calls {sorted(offending)}; Phase 9 must not write, "
            f"execute, or otherwise modify anything"
        )


def test_phase9_uses_the_constrained_reader_and_nothing_else() -> None:
    """The only file access Phase 9 may perform is the reviewed reader.

    If the stage or the model ever called ``Path.read_text`` directly, it would
    bypass the secret-file, traversal and size checks.  The reader itself is the
    one place a raw read is allowed, so it is checked separately: ``read_bytes``
    may appear only inside ``read_source_text``.
    """
    backend_root = Path(__file__).resolve().parents[1]

    for relative in ("app/services/fix_proposal_stage.py", "app/models/fix_proposal.py"):
        tree = ast.parse((backend_root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr not in {"read_text", "read_bytes", "open"}, (
                    f"{relative} calls {node.func.attr}() at line {node.lineno}; "
                    f"Phase 9 must reach the filesystem only through "
                    f"read_source_text, so path and secret validation apply"
                )

    proposer = ast.parse(
        (backend_root / "app/analysis/fix_proposer.py").read_text(encoding="utf-8")
    )
    reader = next(
        node
        for node in ast.walk(proposer)
        if isinstance(node, ast.FunctionDef) and node.name == "read_source_text"
    )
    raw_reads = [
        node.lineno
        for node in ast.walk(reader)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"read_text", "read_bytes"}
    ]
    assert len(raw_reads) == 1, (
        f"read_source_text must perform exactly one raw read, found {raw_reads}"
    )


# ── Registry and state-machine wiring ─────────────────────────────────────────


def test_default_registry_places_phase9_stages_in_progression_order() -> None:
    stages = build_default_stages()
    assert isinstance(stages[WorkflowState.ROOT_CAUSE_READY], RootCauseReadyStage)
    assert isinstance(stages[WorkflowState.FIX_PROPOSAL], FixProposalStage)
    order = [state for state in LINEAR_PROGRESSION if state in stages]
    assert list(stages.keys()) == order


def test_fix_proposal_ready_is_still_a_placeholder(tmp_path: Path) -> None:
    """Phase 9 must not advance past human review."""
    stage = build_default_stages()[WorkflowState.FIX_PROPOSAL_READY]
    result = stage.execute(
        make_context(
            make_run(str(bug_repo(tmp_path)), state=WorkflowState.FIX_PROPOSAL_READY)
        )
    )
    assert result.outcome is StageOutcome.PENDING
    assert result.next_state is None
    assert result.data["implemented"] is False
    assert result.evidence == []


def test_fix_pending_state_no_longer_exists() -> None:
    """The state was renamed, not aliased.

    Keeping a ``FIX_PENDING`` alias would let a client depend on a name that no
    longer describes anything, and would blur the boundary between the Phase 9
    states that exist and the one that never did.
    """
    assert not hasattr(WorkflowState, "FIX_PENDING")
    assert {"FIX_PROPOSAL", "FIX_PROPOSAL_READY"} <= {s.name for s in WorkflowState}
    assert not any(
        state.name == "FIX_PENDING" for state in ALLOWED_TRANSITIONS
    )


def test_progression_runs_root_cause_ready_into_the_approval_gate() -> None:
    start = LINEAR_PROGRESSION.index(WorkflowState.ROOT_CAUSE_READY)
    assert LINEAR_PROGRESSION[start : start + 3] == (
        WorkflowState.ROOT_CAUSE_READY,
        WorkflowState.FIX_PROPOSAL,
        WorkflowState.FIX_PROPOSAL_READY,
    )
    assert WorkflowStateMachine.next_state(WorkflowState.ROOT_CAUSE_READY) is (
        WorkflowState.FIX_PROPOSAL
    )
    assert WorkflowStateMachine.next_state(WorkflowState.FIX_PROPOSAL) is (
        WorkflowState.FIX_PROPOSAL_READY
    )


def test_transitions_follow_the_progression() -> None:
    """Both Phase 9 edges are legal, and the gate is not a shortcut."""
    assert WorkflowStateMachine.can_transition(
        WorkflowState.ROOT_CAUSE_READY, WorkflowState.FIX_PROPOSAL
    )
    assert WorkflowStateMachine.can_transition(
        WorkflowState.FIX_PROPOSAL, WorkflowState.FIX_PROPOSAL_READY
    )
    # The orchestrator would accept the result, so assert it here too.
    WorkflowStateMachine.assert_transition(
        WorkflowState.ROOT_CAUSE_READY, WorkflowState.FIX_PROPOSAL
    )
    # Phase 9 must not invent a way past the gate.
    for target in (WorkflowState.COMPLETED, WorkflowState.ROOT_CAUSE_ANALYSIS):
        assert not WorkflowStateMachine.can_transition(
            WorkflowState.FIX_PROPOSAL_READY, target
        )
