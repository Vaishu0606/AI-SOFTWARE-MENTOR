"""
Phase 10 REST API tests — ``GET /api/mentor/runs/{run_id}/test-plan``.

Covers the endpoint contract: the 409 that every real run returns today, the
honesty fields that cannot be softened, and the guarantee that no write verb
exists anywhere near a plan.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.mentor import TestPlanResponse
from app.main import app
from app.models.test_plan import TestPlanStatus
from app.models.workflow import WorkflowEventType, WorkflowState
from app.services.fix_approval import FixApprovalRejected
from app.services.orchestrator import MentorOrchestrator
from app.services.test_plan_stage import (
    TEST_PLAN_CREATED_AT_KEY,
    TEST_PLAN_KEY,
)

client = TestClient(app)

ISSUE = "Checkout is failing when a discount code is applied."
UNKNOWN_RUN_ID = "00000000-0000-4000-8000-000000000000"
BUGGY = "    return int(subtotal_cents / 100) * code.value"


def _build_app(root: Path) -> None:
    """The application files containing exactly the defect Phase 8 diagnoses."""
    (root / "app" / "services").mkdir(parents=True)
    (root / "app" / "routes").mkdir(parents=True)
    (root / "app" / "main.py").write_text("def run():\n    return True\n", encoding="utf-8")
    (root / "app" / "services" / "discount_service.py").write_text(
        "def calculate_discount_cents(subtotal_cents, code):\n" + BUGGY + "\n",
        encoding="utf-8",
    )
    (root / "app" / "routes" / "checkout.py").write_text(
        "from app.services.discount_service import calculate_discount_cents\n",
        encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The defective application plus one test file.

    The test file matters: without a recorded test-suite layout the planner
    honestly declines, so a fixture like this is the only way to exercise a real
    plan end to end.
    """
    root = tmp_path / "ApiRepo"
    _build_app(root)
    (root / "tests").mkdir()
    (root / "tests" / "test_discount.py").write_text(
        "from app.services.discount_service import calculate_discount_cents\n\n"
        "def test_apply_discount():\n    pass\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def untested_repo(tmp_path: Path) -> Path:
    """The same application code, but no test file anywhere.

    A real end-to-end negative: the plan comes back declined, so the endpoint's
    200-with-a-reason path is exercised against live data rather than a stored
    fixture.
    """
    root = tmp_path / "NoTests"
    _build_app(root)
    return root


def _create(repo_path: Path, issue: str = ISSUE) -> str:
    response = client.post(
        "/api/mentor/runs",
        json={"repository_path": str(repo_path), "issue": issue},
    )
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


def _get(run_id: str):
    return client.get(f"/api/mentor/runs/{run_id}/test-plan")


def _store(run_id: str, **context: object) -> None:
    """Overwrite a run's stored context, to reach states an end-to-end run cannot."""
    orchestrator = MentorOrchestrator()
    run = orchestrator.get_run(run_id)
    orchestrator._runs.save(run.model_copy(update={"context": {**run.context, **context}}))


def _approve_and_reach_test_plan(run_id: str) -> None:
    """Approve the fix and run up to and including the Phase 10 stage.

    This replaced Phase 10's ``_force_past_the_gate``.  That helper relied on
    ``FIX_PROPOSAL_READY → TEST_PLAN`` being a legal edge, which it no longer is:
    the gate now leads only to ``FIX_APPROVED``, and no stage is registered for
    ``FIX_APPROVED``, so a run genuinely cannot get past it without a human.

    The replacement walks the real path — approve, write, confirm, plan — and
    stops one step short of verification so these tests stay about Phase 10.  It
    writes to the per-test ``tmp_path`` fixture repository, which is exactly what
    the system is for.
    """
    orchestrator = MentorOrchestrator()
    orchestrator.approve_fix(
        run_id, approved_by="phase10-tests", comment="reaching TEST_PLAN"
    )
    orchestrator.advance(run_id)  # FIX_APPLYING
    orchestrator.advance(run_id)  # FIX_APPLIED
    orchestrator.advance(run_id)  # TEST_PLAN
    assert (
        orchestrator.get_run(run_id).current_state is WorkflowState.TEST_PLAN
    ), "the approval path did not reach the planner"


def _force_past_the_gate(run_id: str) -> None:
    """Kept as the old name so the Phase 10 tests read unchanged.

    It is no longer a shortcut — see ``_approve_and_reach_test_plan``.
    """
    _approve_and_reach_test_plan(run_id)


def _events(run_id: str):
    return MentorOrchestrator()._events.list(run_id)


# ── The default answer is 409 ─────────────────────────────────────────────────


def test_a_real_run_never_reaches_a_plan(repo: Path) -> None:
    """The human approval gate is in front of Phase 10, so every real run is 409.

    This is the single most important fact about the endpoint: a client cannot
    obtain a plan by creating a run, because reaching ``TEST_PLAN`` requires a
    human to have approved the proposed fix.
    """
    run_id = _create(repo)
    assert MentorOrchestrator().get_run(run_id).current_state is (
        WorkflowState.FIX_PROPOSAL_READY
    )
    response = _get(run_id)
    assert response.status_code == 409
    assert "no test plan" in response.json()["detail"]


def test_the_409_detail_names_the_state_and_the_gate(repo: Path) -> None:
    detail = _get(_create(repo)).json()["detail"]
    assert "FIX_PROPOSAL_READY" in detail
    assert "TEST_PLAN" in detail
    assert "human approval gate" in detail


# ── The 200 path, reached only by forcing the gate ────────────────────────────


def test_a_planned_run_returns_its_plan(repo: Path) -> None:
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    response = _get(run_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run_id"] == run_id
    assert body["issue"] == ISSUE
    assert body["status"] == TestPlanStatus.READY_FOR_REVIEW.value
    assert body["ready_for_review"] is True
    assert body["rationale"]
    assert body["scenarios"]
    assert body["coverage_gaps"]


def test_every_scenario_names_a_file_and_asserts_something(repo: Path) -> None:
    """A scenario a developer cannot act on is not a plan."""
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    for scenario in _get(run_id).json()["scenarios"]:
        assert scenario["suggested_file"]
        assert scenario["file_status"] in {"EXISTING", "PROPOSED_NEW"}
        assert scenario["steps"]
        assert scenario["expected_behavior"]


def test_a_run_with_no_proposal_cannot_be_approved_or_planned(
    untested_repo: Path,
) -> None:
    """No diff to review means no approval, so no plan.

    Phase 11 made this reachable as a real end-to-end negative.  Without the tests
    that make the defect diagnosable, Phase 8 records no root cause and Phase 9
    therefore produces ``INSUFFICIENT_EVIDENCE`` with no diff.  There is nothing
    for a human to approve, so the workflow correctly stops at the gate and the
    endpoint keeps answering 409 — rather than the run being pushed through an
    approval of nothing in order to reach a 200.
    """
    run_id = _create(untested_repo, "Why is the cart empty?")

    with pytest.raises(FixApprovalRejected) as excinfo:
        MentorOrchestrator().approve_fix(run_id, approved_by="reviewer")
    assert "INSUFFICIENT_EVIDENCE" in excinfo.value.reason

    run = MentorOrchestrator().get_run(run_id)
    assert run.current_state is WorkflowState.FIX_PROPOSAL_READY
    assert _get(run_id).status_code == 409



def test_a_declined_plan_emits_no_scenario_evidence(repo: Path) -> None:
    """No scenario, no proposal - the feed must not carry an empty proposal.

    A declined plan is no longer reachable by a live run: a run that gets as far
    as a declined plan had a diffable proposal, and a diffable proposal can be
    approved, and approving it reaches ``TEST_PLAN`` with a plan.  The declined
    shape is therefore injected, the way its sibling test does, and what is being
    checked is the endpoint's rendering of it rather than how a run gets there.
    """
    run_id = _create(repo)
    _store(
        run_id,
        **{
            TEST_PLAN_KEY: {
                "issue": ISSUE,
                "status": TestPlanStatus.INSUFFICIENT_EVIDENCE.value,
                "rationale": "No record of the test suite's layout is available.",
                "scenarios": [],
            }
        },
    )
    assert _get(run_id).status_code == 200
    detail = client.get(f"/api/mentor/runs/{run_id}").json()
    assert [e for e in detail["evidence"] if e["agent"] == "test_planner"] == []



def test_a_stored_plan_survives_a_corrupt_summary_as_absent(repo: Path) -> None:
    run_id = _create(repo)
    _store(run_id, **{TEST_PLAN_KEY: {"garbage": True}})
    assert _get(run_id).status_code == 409


def test_a_non_dict_stored_plan_reads_as_absent(repo: Path) -> None:
    run_id = _create(repo)
    _store(run_id, **{TEST_PLAN_KEY: ["not", "a", "plan"]})
    assert _get(run_id).status_code == 409


def test_a_declined_plan_is_200_with_no_scenarios(repo: Path) -> None:
    """`INSUFFICIENT_EVIDENCE` must not be renderable as "there is nothing to test"."""
    run_id = _create(repo)
    _store(
        run_id,
        **{
            TEST_PLAN_KEY: {
                "issue": ISSUE,
                "status": TestPlanStatus.INSUFFICIENT_EVIDENCE.value,
                "rationale": "No record of the test suite's layout is available.",
                "scenarios": [],
            }
        },
    )
    body = _get(run_id).json()
    assert body["status"] == TestPlanStatus.INSUFFICIENT_EVIDENCE.value
    assert body["ready_for_review"] is False
    assert body["scenarios"] == []
    assert body["rationale"]


# ── The honesty fields ────────────────────────────────────────────────────────


def test_response_never_claims_a_test_was_written_or_run(repo: Path) -> None:
    """Hard-wired from literals, so no future model edit could soften them."""
    for run_id in (_create(repo),):
        _force_past_the_gate(run_id)
        body = _get(run_id).json()
        assert body["tests_executed"] is False
        assert body["test_source_written"] is False
        assert body["execution_status"] == "NOT_RUN"


def test_the_execution_status_field_cannot_express_a_pass(repo: Path) -> None:
    """A real end-to-end plan, so the assertion is about live data."""
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    body = _get(run_id).json()
    assert body["execution_status"] == "NOT_RUN"
    assert "PASSED" not in body["execution_status"]


def test_the_response_model_types_execution_status_as_a_string(repo: Path) -> None:
    """The response cannot widen the enum into something a UI could treat as a pass."""
    field = TestPlanResponse.model_fields["execution_status"]
    assert field.annotation is str


def test_ready_for_review_agrees_with_the_scenarios(repo: Path) -> None:
    """A client can trust this pair without cross-checking anything else."""
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    body = _get(run_id).json()
    if body["ready_for_review"]:
        assert body["scenarios"]
        assert body["status"] == TestPlanStatus.READY_FOR_REVIEW.value
    else:
        assert body["scenarios"] == []


def test_gaps_without_a_scenario_are_still_reported(repo: Path) -> None:
    """An unclosed gap is an open item, and the response must not hide it."""
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    body = _get(run_id).json()
    covered = {gap for scenario in body["scenarios"] for gap in scenario["covers_gaps"]}
    assert covered, "a real plan should close at least one recorded gap"
    open_gaps = [g["id"] for g in body["coverage_gaps"] if g["id"] not in covered]
    assert open_gaps, "this fixture is expected to leave some gaps open"
    assert any("no outlined scenario" in limitation for limitation in body["limitations"])
    assert all(gap in " ".join(body["limitations"]) for gap in open_gaps)


def test_every_gap_cites_evidence_and_no_gap_claims_verification(repo: Path) -> None:
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    body = _get(run_id).json()
    for gap in body["coverage_gaps"]:
        assert gap["evidence_ids"]
        assert "VERIFIED" not in gap["kind"]


def test_the_anchor_is_a_location_and_never_carries_source(repo: Path) -> None:
    """The response model has no excerpt field, so a plan cannot re-quote code."""
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    anchor = _get(run_id).json()["anchor"]
    if anchor is not None:
        assert set(anchor) == {"file_path", "line", "function"}
        assert "excerpt" not in anchor
        assert BUGGY.strip() not in str(anchor)


def test_nothing_in_the_plan_is_typed_as_a_verified_result(repo: Path) -> None:
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    body = _get(run_id).json()
    detail = client.get(f"/api/mentor/runs/{run_id}").json()
    assert all(e["evidence_type"] != "VERIFIED_RESULT" for e in detail["evidence"])


def test_planner_evidence_is_proposed_actions_only(repo: Path) -> None:
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    detail = client.get(f"/api/mentor/runs/{run_id}").json()
    planner = [e for e in detail["evidence"] if e["agent"] == "test_planner"]
    assert planner, "forcing past the gate must run the planner"
    assert {e["evidence_type"] for e in planner} == {"PROPOSED_ACTION"}


# ── Determinism and read-only guarantees ──────────────────────────────────────


def test_endpoint_is_idempotent(repo: Path) -> None:
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    assert _get(run_id).json() == _get(run_id).json()


def test_response_reports_a_creation_time_separate_from_the_model(repo: Path) -> None:
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    body = _get(run_id).json()
    assert body["created_at"]
    stored = MentorOrchestrator().get_run(run_id)
    assert stored.context[TEST_PLAN_CREATED_AT_KEY] == body["created_at"]
    assert "created_at" not in stored.context[TEST_PLAN_KEY]


def test_reading_a_plan_does_not_change_the_repository(repo: Path) -> None:
    """The plan endpoint is read-only, even though the run behind it now writes.

    Phase 11 changed what this run does *before* the plan exists — a human approval
    applies the fix — so "the repository is byte-identical afterwards" is no longer
    the assertion.  The claim that actually matters is narrower and survives: the
    Phase 10 stage writes no test source, and reading a plan writes nothing either.
    The snapshot is therefore taken after the plan exists, so the only thing the
    comparison covers is the plan and the read.
    """
    run_id = _create(repo)
    _force_past_the_gate(run_id)

    target = repo / "app" / "services" / "discount_service.py"
    # Phase 11 did apply the approved fix, and the planner did not undo it.
    assert BUGGY not in target.read_text(encoding="utf-8")

    before = {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()}
    _get(run_id)
    after = {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()}

    assert after == before
    assert not (repo / "tests" / "test_regression_discount.py").exists()


def test_no_endpoint_can_run_or_create_a_test(repo: Path) -> None:
    """No write verb exists near a plan, and no route to run one."""
    run_id = _create(repo)
    for method, path in [
        ("post", f"/api/mentor/runs/{run_id}/test-plan"),
        ("put", f"/api/mentor/runs/{run_id}/test-plan"),
        ("patch", f"/api/mentor/runs/{run_id}/test-plan"),
        ("delete", f"/api/mentor/runs/{run_id}/test-plan"),
        ("post", f"/api/mentor/runs/{run_id}/test-plan/run"),
        ("post", f"/api/mentor/runs/{run_id}/test-plan/apply"),
        ("post", f"/api/mentor/runs/{run_id}/test-plan/write"),
    ]:
        response = getattr(client, method)(path)
        assert response.status_code in (404, 405), (
            f"{method.upper()} {path} returned {response.status_code}"
        )


# ── 400 / 404 ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "12345", "%20", "ABCDEF"])
def test_malformed_run_id_returns_400(bad_id: str) -> None:
    assert _get(bad_id).status_code == 400


def test_unknown_run_returns_404() -> None:
    assert _get(UNKNOWN_RUN_ID).status_code == 404


def test_404_detail_names_the_run() -> None:
    assert UNKNOWN_RUN_ID in _get(UNKNOWN_RUN_ID).json()["detail"]


def test_run_id_is_a_server_issued_uuid(repo: Path) -> None:
    assert uuid.UUID(_create(repo))


# ── Events ────────────────────────────────────────────────────────────────────


def test_a_run_that_never_plans_emits_no_phase10_events(repo: Path) -> None:
    """The gate is in front of the stage, so the events cannot appear at all."""
    run_id = _create(repo)
    types = {event.event_type for event in _events(run_id)}
    assert WorkflowEventType.TEST_PLAN_COMPLETED.value not in types


def test_forcing_past_the_gate_emits_the_phase10_events(repo: Path) -> None:
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    types = {event.event_type for event in _events(run_id)}
    assert WorkflowEventType.TEST_PLAN_STARTED.value in types
    assert WorkflowEventType.TEST_PLAN_COMPLETED.value in types
    assert WorkflowEventType.TEST_PLAN_FAILED.value not in types


def test_the_completed_event_admits_nothing_was_written_or_run(repo: Path) -> None:
    run_id = _create(repo)
    _force_past_the_gate(run_id)
    completed = [
        event
        for event in _events(run_id)
        if event.event_type == WorkflowEventType.TEST_PLAN_COMPLETED.value
    ]
    assert len(completed) == 1
    payload = completed[0].payload
    assert payload["tests_executed"] is False
    assert payload["test_source_written"] is False
    assert payload["execution_status"] == "NOT_RUN"
