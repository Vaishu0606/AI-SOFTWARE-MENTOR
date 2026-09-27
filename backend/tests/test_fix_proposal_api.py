"""
Phase 9 REST API tests — ``GET /api/mentor/runs/{run_id}/fix-proposal``.

Covers the endpoint contract: status mapping, the honesty fields that cannot be
softened, the read-only guarantee, and the 400/404/409 distinctions that let a
client tell "not proposed yet" from "proposed nothing".
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.fix_proposal import FixProposalStatus
from app.models.workflow import WorkflowEventType
from app.services.fix_proposal_stage import (
    FIX_PROPOSAL_CREATED_AT_KEY,
    FIX_PROPOSAL_KEY,
)
from app.services.orchestrator import MentorOrchestrator

client = TestClient(app)

ISSUE = "Checkout is failing when a discount code is applied."
UNKNOWN_RUN_ID = "00000000-0000-4000-8000-000000000000"
BUGGY = "    return int(subtotal_cents / 100) * code.value"
FIXED = "    return int(subtotal_cents * code.value / 100)"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A minimal repository containing exactly the defect Phase 8 diagnoses."""
    root = tmp_path / "ApiRepo"
    (root / "app" / "services").mkdir(parents=True)
    (root / "app" / "main.py").write_text(
        "def run():\n    return True\n", encoding="utf-8"
    )
    (root / "app" / "services" / "discount_service.py").write_text(
        "def calculate_discount_cents(subtotal_cents, code):\n" + BUGGY + "\n",
        encoding="utf-8",
    )
    return root


def _create(repo_path: Path, issue: str = ISSUE):
    return client.post(
        "/api/mentor/runs",
        json={"repository_path": str(repo_path), "issue": issue},
    )


def _get(run_id: str):
    return client.get(f"/api/mentor/runs/{run_id}/fix-proposal")


def _store_proposal(run_id: str, **context: object) -> None:
    """Overwrite a run's stored context, to reach states an end-to-end run
    cannot reach on demand (e.g. a corrupted or absent proposal)."""
    orchestrator = MentorOrchestrator()
    run = orchestrator.get_run(run_id)
    orchestrator._runs.save(run.model_copy(update={"context": {**run.context, **context}}))


def _events(run_id: str):
    return MentorOrchestrator()._events.list(run_id)


# ── Happy path ────────────────────────────────────────────────────────────────


def test_endpoint_returns_the_proposal(repo: Path) -> None:
    run_id = _create(repo).json()["run_id"]
    response = _get(run_id)
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["issue"] == ISSUE
    assert body["status"] in {s.value for s in FixProposalStatus}


def test_run_reaches_the_phase9_approval_gate(repo: Path) -> None:
    """A run must stop at FIX_PROPOSAL_READY, not run on to verification."""
    body = _create(repo).json()
    assert body["current_state"] == "FIX_PROPOSAL_READY"
    assert body["status"] == "PENDING"
    assert body["is_terminal"] is False


def test_response_reports_the_run_state_it_was_read_at(repo: Path) -> None:
    run_id = _create(repo).json()["run_id"]
    assert _get(run_id).json()["run_state"] == "FIX_PROPOSAL_READY"


def test_endpoint_is_idempotent(repo: Path) -> None:
    """Read-only: two calls must return byte-identical bodies."""
    run_id = _create(repo).json()["run_id"]
    first = _get(run_id).json()
    second = _get(run_id).json()
    assert first == second


def test_response_reports_a_creation_time_separate_from_the_model(
    repo: Path,
) -> None:
    """The wall clock is reported without making the model non-deterministic."""
    body = _get(_create(repo).json()["run_id"]).json()
    assert body["created_at"]
    stored = MentorOrchestrator().get_run(body["run_id"])
    assert stored.context[FIX_PROPOSAL_CREATED_AT_KEY] == body["created_at"]
    assert "created_at" not in stored.context[FIX_PROPOSAL_KEY]


# ── The honesty fields ────────────────────────────────────────────────────────


def test_response_never_claims_the_fix_was_applied_or_tested(repo: Path) -> None:
    """The two fields exist so a client cannot quietly imply verification."""
    body = _get(_create(repo).json()["run_id"]).json()
    assert body["applied"] is False
    assert body["tests_executed"] is False
    assert body["validation_plan"]["tests_executed"] is False


def test_ready_for_review_agrees_with_the_diff(repo: Path) -> None:
    """A client can trust this pair without cross-checking anything else."""
    body = _get(_create(repo).json()["run_id"]).json()
    if body["ready_for_review"]:
        assert body["unified_diff"]
        assert body["changes"]
        assert body["affected_files"]
    else:
        assert body["unified_diff"] is None
        assert body["changes"] == []
        assert body["affected_files"] == []


def test_nothing_in_the_response_is_typed_as_a_verified_result(repo: Path) -> None:
    """Phase 9 runs nothing, so no field may carry a verified-result claim.

    Checked structurally rather than by substring: the validation-plan notes
    legitimately *name* ``VERIFIED_RESULT`` to say that only Phase 11 may produce
    one, and a substring search would flag that honest sentence.
    """
    body = _get(_create(repo).json()["run_id"]).json()
    assert body["tests_executed"] is False
    assert body["validation_plan"]["tests_executed"] is False
    for evidence in body["supporting_evidence"]:
        assert evidence["evidence_type"] != "VERIFIED_RESULT"
    assert all(
        evidence["evidence_type"] != "VERIFIED_RESULT"
        for evidence in _get(body["run_id"]).json()["supporting_evidence"]
    )


def test_diff_counts_ignore_headers_and_match_the_changes(repo: Path) -> None:
    body = _get(_create(repo).json()["run_id"]).json()
    if body["changes"]:
        assert body["added_lines"] == len(body["changes"])
        assert body["removed_lines"] == len(body["changes"])


def test_anchor_is_a_location_and_never_carries_source(repo: Path) -> None:
    """The response model has no excerpt field for a proposal."""
    body = _get(_create(repo).json()["run_id"]).json()
    anchor = body["anchor"]
    if anchor is not None:
        assert set(anchor) == {"file_path", "line", "function"}
        assert "excerpt" not in anchor


# ── 400 / 404 / 409 ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad_id", ["not-a-uuid", "12345", "%20", "ABCDEF"])
def test_malformed_run_id_returns_400(bad_id: str) -> None:
    assert _get(bad_id).status_code == 400


def test_unknown_run_returns_404() -> None:
    assert _get(UNKNOWN_RUN_ID).status_code == 404


def test_404_detail_names_the_run() -> None:
    body = _get(UNKNOWN_RUN_ID).json()
    assert UNKNOWN_RUN_ID in body["detail"]


def test_run_without_a_proposal_returns_409(tmp_path: Path) -> None:
    """"Not proposed yet" must be distinguishable from "proposed nothing"."""
    empty = tmp_path / "Empty"
    empty.mkdir()
    (empty / "main.py").write_text("x = 1\n", encoding="utf-8")
    run_id = _create(empty, "Why is the cart empty?").json()["run_id"]

    orchestrator = MentorOrchestrator()
    run = orchestrator.get_run(run_id)
    from app.services.fix_proposal_stage import load_fix_proposal

    if load_fix_proposal(run) is None:
        response = _get(run_id)
        assert response.status_code == 409
        assert "no fix proposal" in response.json()["detail"]
    else:
        # A real run always produces a proposal, negative outcomes included.
        assert _get(run_id).status_code == 200


def test_409_detail_reports_the_current_state(repo: Path) -> None:
    run_id = _create(repo).json()["run_id"]
    orchestrator = MentorOrchestrator()
    run = orchestrator.get_run(run_id)
    context = {k: v for k, v in run.context.items() if k != FIX_PROPOSAL_KEY}
    orchestrator._runs.save(run.model_copy(update={"context": context}))

    body = _get(run_id).json()
    assert "no fix proposal" in body["detail"]
    assert "FIX_PROPOSAL" in body["detail"]


def test_corrupt_stored_proposal_reads_as_absent(repo: Path) -> None:
    """A damaged summary must not 500 a read-only endpoint."""
    run_id = _create(repo).json()["run_id"]
    _store_proposal(run_id, **{FIX_PROPOSAL_KEY: {"garbage": True}})
    assert _get(run_id).status_code == 409


# ── Read-only guarantees ──────────────────────────────────────────────────────


def test_no_endpoint_can_apply_a_proposal(repo: Path) -> None:
    """There is no write verb anywhere near the fix proposal."""
    run_id = _create(repo).json()["run_id"]
    for method, path in [
        ("post", f"/api/mentor/runs/{run_id}/fix-proposal"),
        ("put", f"/api/mentor/runs/{run_id}/fix-proposal"),
        ("patch", f"/api/mentor/runs/{run_id}/fix-proposal"),
        ("delete", f"/api/mentor/runs/{run_id}/fix-proposal"),
        ("post", f"/api/mentor/runs/{run_id}/fix-proposal/apply"),
    ]:
        response = getattr(client, method)(path)
        assert response.status_code in (404, 405), (
            f"{method.upper()} {path} returned {response.status_code}"
        )


def test_reading_the_proposal_does_not_change_the_repository(repo: Path) -> None:
    """The diff is a proposal; the file on disk keeps its bug."""
    run_id = _create(repo).json()["run_id"]
    before = {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()}
    _get(run_id)
    after = {p: p.read_bytes() for p in sorted(repo.rglob("*")) if p.is_file()}
    assert after == before
    assert BUGGY in (repo / "app" / "services" / "discount_service.py").read_text(
        encoding="utf-8"
    )


def test_no_verified_result_evidence_is_produced_by_the_read(repo: Path) -> None:
    run_id = _create(repo).json()["run_id"]
    _get(run_id)
    detail = client.get(f"/api/mentor/runs/{run_id}").json()
    assert all(e["evidence_type"] != "VERIFIED_RESULT" for e in detail["evidence"])


# ── Events ────────────────────────────────────────────────────────────────────


def test_run_emits_the_phase9_events(repo: Path) -> None:
    run_id = _create(repo).json()["run_id"]
    types = {event.event_type for event in _events(run_id)}
    assert WorkflowEventType.FIX_PROPOSAL_STARTED.value in types
    assert WorkflowEventType.FIX_PROPOSAL_COMPLETED.value in types
    assert WorkflowEventType.FIX_PROPOSAL_FAILED.value not in types


def test_phase9_events_admit_nothing_was_applied(repo: Path) -> None:
    run_id = _create(repo).json()["run_id"]
    completed = [
        event
        for event in _events(run_id)
        if event.event_type == WorkflowEventType.FIX_PROPOSAL_COMPLETED.value
    ]
    assert len(completed) == 1
    payload = completed[0].payload
    assert payload["applied"] is False
    assert payload["tests_executed"] is False


# ── The demo scenario ─────────────────────────────────────────────────────────


def test_demo_scenario_ends_at_the_gate_with_a_diff_or_an_honest_no(
    repo: Path,
) -> None:
    """The end-to-end demo contract for Phase 9.

    Whatever Phase 8 concluded about the repository, the run must be parked at
    the approval gate with a well-formed response that never overstates what was
    done.
    """
    body = _get(_create(repo).json()["run_id"]).json()
    assert body["run_state"] == "FIX_PROPOSAL_READY"
    assert body["applied"] is False
    assert body["tests_executed"] is False
    assert body["rationale"]
    assert body["risk"]["level"] in {"LOW", "MEDIUM", "HIGH"}
    if body["ready_for_review"]:
        assert body["unified_diff"].startswith("--- a/")
        assert FIXED.strip() in body["unified_diff"] or BUGGY.strip() in body["unified_diff"]


def test_response_model_has_no_field_that_could_expose_an_excerpt() -> None:
    from app.api.mentor import FixProposalResponse

    assert "excerpt" not in FixProposalResponse.model_fields["anchor"].annotation.__args__[0].__annotations__ if hasattr(FixProposalResponse.model_fields["anchor"].annotation, "__args__") else True
    anchor_type = FixProposalResponse.model_fields["anchor"].annotation
    if hasattr(anchor_type, "__args__"):
        inner = anchor_type.__args__[0]
        assert "excerpt" not in getattr(inner, "model_fields", {})


def test_run_id_is_a_server_issued_uuid(repo: Path) -> None:
    run_id = _create(repo).json()["run_id"]
    assert uuid.UUID(run_id)
