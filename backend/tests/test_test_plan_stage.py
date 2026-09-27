"""
Phase 10 tests — the test-plan stage and the workflow hand-off.

Covers the stage layer rather than the planner (which ``test_test_planner``
owns): evidence typing, context persistence and retrieval, event emission, the
absence of any repository access, and the guarantee that the human approval gate
in front of this stage is still a placeholder.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.models.domain import Evidence, EvidenceType
from app.models.fix_proposal import FixProposalStatus
from app.models.root_cause import (
    ConfidenceBreakdown,
    ConfidenceLevel,
    RootCauseAnalysis,
    RootCauseKind,
    RootCauseStatus,
    SourceAnchor,
)
from app.models.test_plan import TestExecutionStatus, TestPlanStatus
from app.models.workflow import (
    MentorRun,
    RunStatus,
    StageOutcome,
    StageResult,
    WorkflowState,
)
from app.services.fix_proposal_stage import (
    FIX_PROPOSAL_CREATED_AT_KEY,
    FIX_PROPOSAL_KEY,
)
from app.services.root_cause_stage import ROOT_CAUSE_KEY
from app.services.stages import (
    PlaceholderStage,
    StageContext,
    build_default_stages,
)
from app.services.state_machine import ALLOWED_TRANSITIONS, LINEAR_PROGRESSION
from app.services.test_plan_stage import (
    PLANNER_AGENT,
    TEST_PLAN_CREATED_AT_KEY,
    TEST_PLAN_KEY,
    TestPlanStage,
    load_test_plan,
)

ISSUE = "Checkout is failing when a discount code is applied."
RUN_ID = "11111111-1111-4111-8111-111111111111"
ANCHOR_FILE = "app/services/discount_service.py"


# ── Fixtures / builders ───────────────────────────────────────────────────────


def stored_analysis(
    run_id: str = RUN_ID,
    *,
    kind: RootCauseKind | None = RootCauseKind.TRUNCATION_BEFORE_SCALING,
    with_anchor: bool = True,
) -> RootCauseAnalysis:
    """A Phase 8 analysis exactly as the real stage persists it."""
    determined = kind is not None
    return RootCauseAnalysis(
        run_id=run_id,
        issue=ISSUE,
        status=RootCauseStatus.DETERMINED if determined else RootCauseStatus.INSUFFICIENT_EVIDENCE,
        root_cause="The value is truncated before it is scaled." if determined else None,
        root_cause_kind=kind,
        anchor=(
            SourceAnchor(
                file_path=ANCHOR_FILE,
                line=52,
                function="calculate_discount_cents",
                excerpt="return int(subtotal_cents / 100) * code.value",
            )
            if with_anchor
            else None
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
    )


def coverage_evidence(session_id: str = RUN_ID) -> list[Evidence]:
    """The two halves of Phase 7's coverage picture, as the investigators publish it."""

    def item(**kwargs) -> Evidence:
        return Evidence(session_id=session_id, agent="test_investigator", **kwargs)

    return [
        item(
            id="ev-import",
            evidence_type=EvidenceType.OBSERVATION,
            title="tests/test_discount.py imports 2 production file(s)",
            description="apps/services/discount_service.py, app/routes/checkout.py",
            source_file="tests/test_discount.py",
            metadata={
                "method": "import_graph",
                "files_under_test": [ANCHOR_FILE, "app/routes/checkout.py"],
            },
        ),
        item(
            id="ev-pair",
            evidence_type=EvidenceType.HYPOTHESIS,
            title="No test exercises 'checkout' and 'discount' together",
            description="No test imports a production file unique to both sides.",
            confidence=0.55,
            metadata={
                "method": "coverage_analysis",
                "concepts": ["checkout", "discount"],
                "first_modules": ["app.routes.checkout"],
                "second_modules": ["app.services.discount_service"],
            },
        ),
        item(
            id="ev-boundary",
            evidence_type=EvidenceType.HYPOTHESIS,
            title="the route layer may not be covered at the request boundary",
            description="No test imports a module that declares an HTTP route.",
            confidence=0.6,
            metadata={
                "method": "coverage_analysis",
                "route_files": ["app/routes/checkout.py"],
            },
        ),
    ]


def make_run(
    repository_path: str = "C:/demo/ShopDemo",
    *,
    run_id: str = RUN_ID,
    state: WorkflowState = WorkflowState.TEST_PLAN,
    with_analysis: bool = True,
    with_anchor: bool = True,
    kind: RootCauseKind | None = RootCauseKind.TRUNCATION_BEFORE_SCALING,
    with_proposal: bool = False,
    context: dict | None = None,
) -> MentorRun:
    stored = dict(context or {})
    if with_analysis:
        stored[ROOT_CAUSE_KEY] = stored_analysis(
            run_id, kind=kind, with_anchor=with_anchor
        ).to_summary()
    if with_proposal:
        stored[FIX_PROPOSAL_KEY] = {
            "run_id": run_id,
            "issue": ISSUE,
            "status": FixProposalStatus.READY_FOR_REVIEW.value,
            "rationale": "r",
        }
        stored[FIX_PROPOSAL_CREATED_AT_KEY] = "2026-01-01T00:00:00+00:00"
    return MentorRun(
        run_id=run_id,
        repository_path=repository_path,
        issue=ISSUE,
        current_state=state,
        status=RunStatus.PENDING,
        context=stored,
    )


def make_context(
    run: MentorRun,
    evidence: list[Evidence] | None = None,
    events: list | None = None,
) -> StageContext:
    def emit(event_type: str, message: str, payload: dict) -> None:
        if events is not None:
            events.append((event_type, message, payload))

    return StageContext(
        run=run,
        evidence=coverage_evidence() if evidence is None else evidence,
        data={},
        emit=emit,
    )


# ── The stage contract ────────────────────────────────────────────────────────


def test_the_stage_succeeds_and_requests_only_its_own_state() -> None:
    """A stage may only request its own state; the orchestrator rejects a move."""
    result = TestPlanStage().execute(make_context(make_run()))
    assert result.outcome is StageOutcome.COMPLETED
    assert result.next_state is WorkflowState.TEST_PLAN
    assert result.error is None


def test_the_stage_publishes_the_plan_and_its_wall_clock_separately() -> None:
    """The model is timestamp-free; the clock lives in ``run.context``."""
    data = TestPlanStage().execute(make_context(make_run())).data
    assert TEST_PLAN_KEY in data
    assert data[TEST_PLAN_CREATED_AT_KEY].endswith("+00:00")
    assert "created_at" not in data[TEST_PLAN_KEY]
    assert data["test_plan_status"] == TestPlanStatus.READY_FOR_REVIEW.value
    assert data["test_plan_execution_status"] == TestExecutionStatus.NOT_RUN.value
    assert data["test_plan_gap_count"] == 2
    assert data["test_plan_scenario_count"] == 3


def test_a_declined_plan_is_a_completed_stage_not_a_failure() -> None:
    """"I cannot responsibly plan a test here" is an answer, not an error."""
    events: list = []
    result = TestPlanStage().execute(make_context(make_run(), evidence=[], events=events))
    assert result.outcome is StageOutcome.COMPLETED
    assert result.error is None
    assert result.data["test_plan_status"] == TestPlanStatus.INSUFFICIENT_EVIDENCE.value
    assert result.data["test_plan_scenario_count"] == 0
    assert result.evidence == []
    assert [event_type for event_type, _, _ in events] == [
        "TEST_PLAN_STARTED",
        "TEST_PLAN_COMPLETED",
    ]


def test_a_missing_diagnosis_fails_the_stage_rather_than_opining() -> None:
    """Reaching ``TEST_PLAN`` without an analysis is a wiring fault."""
    events: list = []
    result = TestPlanStage().execute(
        make_context(make_run(with_analysis=False), events=events)
    )
    assert result.outcome is StageOutcome.FAILED
    assert result.next_state is None
    assert "root-cause analysis is recorded" in result.error
    assert [event_type for event_type, _, _ in events] == [
        "TEST_PLAN_STARTED",
        "TEST_PLAN_FAILED",
    ]


def test_a_planner_error_is_reported_rather_than_swallowed() -> None:
    class ExplodingPlanner:
        def plan(self, **_: object):
            raise RuntimeError("boom")

    events: list = []
    result = TestPlanStage(planner=ExplodingPlanner()).execute(  # type: ignore[arg-type]
        make_context(make_run(), events=events)
    )
    assert result.outcome is StageOutcome.FAILED
    assert "RuntimeError: boom" in result.error


def test_a_planner_that_returns_nothing_fails_instead_of_recording_a_plan() -> None:
    """The stage must not persist a plan it never received."""

    class EmptyPlanner:
        def plan(self, **_: object):
            return None

    result = TestPlanStage(planner=EmptyPlanner()).execute(  # type: ignore[arg-type]
        make_context(make_run())
    )
    assert result.outcome is StageOutcome.FAILED
    assert "returned None" in result.error
    assert TEST_PLAN_KEY not in result.data


# ── Evidence typing ───────────────────────────────────────────────────────────


def test_the_stage_emits_only_proposed_actions() -> None:
    """The load-bearing evidence decision.

    The coverage gaps are Phase 7 *hypotheses*.  Re-emitting them here as
    ``OBSERVATION`` would quietly promote an inference to a fact, which is the
    specific failure the evidence type system exists to prevent.
    """
    result = TestPlanStage().execute(make_context(make_run()))
    assert result.evidence
    assert {item.evidence_type for item in result.evidence} == {
        EvidenceType.PROPOSED_ACTION
    }
    assert {item.agent for item in result.evidence} == {PLANNER_AGENT}


def test_the_stage_never_emits_a_verified_result() -> None:
    """Nothing ran, so there is nothing that could have verified anything."""
    result = TestPlanStage().execute(make_context(make_run()))
    assert all(
        item.evidence_type is not EvidenceType.VERIFIED_RESULT for item in result.evidence
    )


def test_one_evidence_item_per_scenario() -> None:
    result = TestPlanStage().execute(make_context(make_run()))
    assert len(result.evidence) == result.data["test_plan_scenario_count"]


def test_each_scenario_item_carries_its_own_not_run_status() -> None:
    """The item itself must not be readable as a result."""
    for item in TestPlanStage().execute(make_context(make_run())).evidence:
        assert item.metadata["execution_status"] == TestExecutionStatus.NOT_RUN.value
        assert item.metadata["tests_executed"] is False
        assert item.confidence is None
        assert "no test source was written" in item.description


def test_the_completed_event_reports_only_false_for_both_flags() -> None:
    events: list = []
    TestPlanStage().execute(make_context(make_run(), events=events))
    payload = next(p for t, _, p in events if t == "TEST_PLAN_COMPLETED")
    assert payload["tests_executed"] is False
    assert payload["test_source_written"] is False
    assert payload["execution_status"] == "NOT_RUN"


# ── Persistence ───────────────────────────────────────────────────────────────


def test_a_recorded_plan_is_reloadable() -> None:
    run = make_run()
    run.context[TEST_PLAN_KEY] = TestPlanStage().execute(make_context(run)).data[TEST_PLAN_KEY]
    plan = load_test_plan(run)
    assert plan is not None
    assert plan.status is TestPlanStatus.READY_FOR_REVIEW
    assert len(plan.scenarios) == 3


def test_a_missing_plan_reads_as_none() -> None:
    assert load_test_plan(make_run()) is None


def test_a_corrupt_plan_reads_as_none_rather_than_raising() -> None:
    """A read-only endpoint should say "unavailable", not 500."""
    run = make_run(context={TEST_PLAN_KEY: {"status": "READY_FOR_REVIEW"}})
    assert load_test_plan(run) is None


def test_a_non_dict_plan_reads_as_none() -> None:
    assert load_test_plan(make_run(context={TEST_PLAN_KEY: ["nope"]})) is None


def test_the_stored_plan_records_the_phase9_proposal_status() -> None:
    run = make_run(with_proposal=True)
    data = TestPlanStage().execute(make_context(run)).data
    assert data[TEST_PLAN_KEY]["fix_proposal_status"] == FixProposalStatus.READY_FOR_REVIEW.value


def test_a_plan_is_produced_without_any_proposal() -> None:
    """A regression test is worth writing before the fix exists."""
    result = TestPlanStage().execute(make_context(make_run(with_proposal=False)))
    assert result.outcome is StageOutcome.COMPLETED
    assert result.data[TEST_PLAN_KEY]["fix_proposal_status"] is None


def test_a_planted_run_id_cannot_be_forged_through_stored_context() -> None:
    """``from_summary`` overwrites the stored ``run_id`` with the run's own."""
    run = make_run(run_id=RUN_ID, context={})
    stored = TestPlanStage().execute(make_context(run)).data[TEST_PLAN_KEY]
    stored["run_id"] = "99999999-9999-4999-8999-999999999999"
    poisoned = make_run(run_id=RUN_ID, context={TEST_PLAN_KEY: stored})
    assert load_test_plan(poisoned).run_id == RUN_ID


# ── The repository is out of reach ────────────────────────────────────────────


def test_the_stage_touches_no_file_in_the_analysed_repository(tmp_path: Path) -> None:
    """Phase 10 is granted no read *or* write of the repository.

    Stronger than Phase 9, which was allowed one constrained read to build a diff
    against the code as it is now.  A plan needs no such thing, and being unable to
    reach the repository is what makes the plan reproducible from stored context
    alone.
    """
    repo = tmp_path / "ShopDemo"
    (repo / "app" / "services").mkdir(parents=True)
    source = repo / "app" / "services" / "discount_service.py"
    source.write_text("def calculate_discount_cents():\n    pass\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()}

    result = TestPlanStage().execute(make_context(make_run(str(repo))))
    assert result.outcome is StageOutcome.COMPLETED

    after = {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()}
    assert after == before
    assert {p.name for p in after} == {"discount_service.py"}


#: Modules introduced by Phase 10.  Held to the same controls as Phases 6, 8 and 9,
#: and to one more: Phase 10 may not even *read* a repository, so a raw read
#: appearing in any of these is itself the violation.
PHASE10_MODULES = [
    "app/models/test_plan.py",
    "app/analysis/test_planner.py",
    "app/services/test_plan_stage.py",
]

#: Bare-name calls that would execute code, matching ``test_phase6_security``.
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}

#: Attribute calls that would read, write or spawn.  Read calls are included here
#: because Phase 10 has no business performing any of them.
FORBIDDEN_ATTRS = {
    "read_text", "read_bytes", "readlines", "open", "glob", "rglob", "iterdir",
    "walk", "exists", "stat", "resolve",
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


def test_no_phase10_module_calls_bare_execution_primitives() -> None:
    backend_root = Path(__file__).resolve().parents[1]
    for relative in PHASE10_MODULES:
        tree = ast.parse((backend_root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in FORBIDDEN_CALLS, (
                    f"{relative} calls {node.func.id!r} at line {node.lineno}"
                )


def test_no_phase10_module_can_read_write_or_execute_anything() -> None:
    """Static guard, mirroring the Phase 6 and Phase 9 security tests.

    Phase 10 reads **nothing**, so this is a superset of the Phase 9 guard: a
    ``read_text`` here is as much a violation as a ``write_text`` in Phase 9 would
    have been.  Every fact the planner uses is already on the run.
    """
    backend_root = Path(__file__).resolve().parents[1]
    for relative in PHASE10_MODULES:
        tree = ast.parse((backend_root / relative).read_text(encoding="utf-8"))
        offending = _called_names(tree) & FORBIDDEN_ATTRS
        assert not offending, (
            f"{relative} calls {sorted(offending)}; Phase 10 must read, write and "
            f"execute nothing — it plans from stored evidence only"
        )


def test_no_phase10_module_imports_a_provider_or_shell() -> None:
    """Phase 10 is deterministic: no LLM, no subprocess."""
    backend_root = Path(__file__).resolve().parents[1]
    banned = {"subprocess", "requests", "httpx", "urllib3", "socket"}
    for relative in PHASE10_MODULES:
        tree = ast.parse((backend_root / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in banned, (
                        f"{relative} imports {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in banned, (
                    f"{relative} imports from {node.module}"
                )


def test_the_api_layer_for_phase10_reads_no_repository_either() -> None:
    """The endpoint maps a stored plan; it must not reach for a repository."""
    backend_root = Path(__file__).resolve().parents[1]
    tree = ast.parse((backend_root / "app/api/mentor.py").read_text(encoding="utf-8"))
    for name in ("read_text", "read_bytes", "write_text", "write_bytes", "open"):
        assert name not in _called_names(tree), f"app/api/mentor.py calls {name}()"


# ── Registry and state-machine wiring ─────────────────────────────────────────


def test_default_registry_installs_the_planner_at_test_plan() -> None:
    stages = build_default_stages()
    assert isinstance(stages[WorkflowState.TEST_PLAN], TestPlanStage)


def test_the_state_before_phase10_is_still_the_human_approval_gate() -> None:
    """The gate must not have been quietly implemented to reach Phase 10.

    Phase 11 did implement the gate — in the orchestrator, not as a stage — so the
    assertion is narrower now: the gate state still has no stage that can cross
    it, and the handler registered there still stalls rather than advancing.
    """
    stages = build_default_stages()
    gate = stages[WorkflowState.FIX_PROPOSAL_READY]
    assert isinstance(gate, PlaceholderStage)

    # A placeholder stalls, and offers nothing: no evidence, no file access, no
    # way past the approval step.
    result = gate.execute(make_context(make_run(state=WorkflowState.FIX_PROPOSAL_READY)))
    assert result.outcome is StageOutcome.PENDING
    assert result.evidence == []
    assert result.next_state is None
    assert gate.state is WorkflowState.FIX_PROPOSAL_READY

    # And the honest reason is the approval endpoint, not "not implemented yet".
    assert result.data["awaiting_human_approval"] is True
    assert "approve-fix" in result.data["approval_endpoint"]
    assert "not implemented" not in result.message.lower()


def test_phase10_is_reached_only_through_the_write() -> None:
    """``TEST_PLAN`` moved after ``FIX_APPLIED``, so the planner runs against a
    repository that already holds the change."""
    stages = build_default_stages()
    start = LINEAR_PROGRESSION.index(WorkflowState.FIX_PROPOSAL_READY)
    assert LINEAR_PROGRESSION[start : start + 5] == (
        WorkflowState.FIX_PROPOSAL_READY,
        WorkflowState.FIX_APPROVED,
        WorkflowState.FIX_APPLYING,
        WorkflowState.FIX_APPLIED,
        WorkflowState.TEST_PLAN,
    )
    # Phase 11 owns the stage after it, and it is a real one.
    verification = stages[LINEAR_PROGRESSION[start + 5]]
    assert verification.state is LINEAR_PROGRESSION[start + 5]
    assert not isinstance(verification, PlaceholderStage)


def test_the_state_is_named_test_plan_not_test_pending() -> None:
    """The rename is deliberate and unaliased, as ``FIX_PENDING`` was."""
    assert not any(state.name == "TEST_PENDING" for state in ALLOWED_TRANSITIONS)
    assert not hasattr(WorkflowState, "TEST_PENDING")


def test_the_stage_cannot_steer_the_workflow() -> None:
    """A stage that requested a different state would be rejected downstream."""
    assert TestPlanStage.state is WorkflowState.TEST_PLAN
    assert StageResult.completed("m", next_state=WorkflowState.COMPLETED).next_state is (
        WorkflowState.COMPLETED
    )
