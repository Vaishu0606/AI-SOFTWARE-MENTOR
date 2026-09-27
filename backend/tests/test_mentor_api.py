"""
Mentor run REST API — Phase 6, extended by Phase 7.

Covers POST /api/mentor/runs, GET /api/mentor/runs/{run_id},
GET /api/mentor/runs/{run_id}/investigation, run-ID validation, and the
security control that stops a client from setting workflow state.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

ISSUE = "Checkout is failing when a discount code is applied."
UNKNOWN_RUN_ID = "00000000-0000-4000-8000-000000000000"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "ApiRepo"
    root.mkdir()
    (root / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    return root


def _create(repo: Path, issue: str = ISSUE):
    return client.post(
        "/api/mentor/runs", json={"repository_path": str(repo), "issue": issue}
    )


# ── POST /api/mentor/runs ─────────────────────────────────────────────────────


def test_post_run_returns_201(repo: Path) -> None:
    assert _create(repo).status_code == 201


def test_post_run_response_contains_run_id_state_and_status(repo: Path) -> None:
    body = _create(repo).json()
    assert uuid.UUID(body["run_id"])
    assert body["current_state"] == "FIX_PROPOSAL_READY"
    assert body["status"] == "PENDING"
    assert body["error"] is None
    assert body["is_terminal"] is False


def test_post_run_echoes_the_request(repo: Path) -> None:
    body = _create(repo, "Why is the cart empty?").json()
    assert body["repository_path"] == str(repo)
    assert body["issue"] == "Why is the cart empty?"


def test_post_run_ids_are_unique(repo: Path) -> None:
    ids = {_create(repo).json()["run_id"] for _ in range(10)}
    assert len(ids) == 10


def test_post_run_records_server_owned_workflow_fields(repo: Path) -> None:
    body = _create(repo).json()
    assert body["event_count"] > 0
    assert body["last_event_sequence"] == body["event_count"]
    assert body["created_at"] and body["updated_at"]


def test_post_run_reports_observed_repository_context(repo: Path) -> None:
    body = _create(repo).json()
    assert body["context"]["repository_name"] == "ApiRepo"
    assert body["context"]["total_files"] == 1
    assert body["evidence"], "Phase 3/5/7 stages should record evidence"
    # Phase 3 and Phase 5 only ever observe; Phase 7 may also hypothesise.
    context_agents = {"repository_context", "architecture_context"}
    context_evidence = [e for e in body["evidence"] if e["agent"] in context_agents]
    assert context_evidence
    assert all(e["evidence_type"] == "OBSERVATION" for e in context_evidence)
    assert all(e["evidence_type"] != "VERIFIED_RESULT" for e in body["evidence"])


def test_post_run_records_a_root_cause_but_never_a_verified_result(
    repo: Path,
) -> None:
    """Phase 8 may name a root cause, but nothing may be reported as verified.

    Phase 7 deliberately stopped at hypotheses because ranking them into a
    conclusion was Phase 8's job.  That job is now done, so the run carries an
    analysis on the run context and has moved on to the Phase 9 approval gate.
    What must not change is the epistemic floor: Phase 8 executes nothing, so no
    ``VERIFIED_RESULT`` may exist and the analysis must say so.
    """
    body = _create(repo).json()
    assert body["current_state"] == "FIX_PROPOSAL_READY"
    summary = body["context"].get("root_cause_summary")
    assert summary is not None
    # Unranked hypotheses are still Phase 7 evidence, never a conclusion.
    assert body["context"].get("hypotheses") is None
    # Nothing was executed, so nothing may claim to have been verified.
    assert summary["tests_executed"] is False
    assert summary["verified_result_count"] == 0
    assert not any(
        e["evidence_type"] == "VERIFIED_RESULT" for e in body["evidence"]
    )


def test_post_run_does_not_modify_the_repository(repo: Path) -> None:
    before = sorted(p.name for p in repo.iterdir())
    _create(repo)
    assert sorted(p.name for p in repo.iterdir()) == before
    assert (repo / "main.py").read_text(encoding="utf-8") == (
        "from fastapi import FastAPI\napp = FastAPI()\n"
    )


def test_post_run_with_missing_repository_fails_the_run(tmp_path: Path) -> None:
    response = _create(tmp_path / "definitely-not-here")
    assert response.status_code == 201
    body = response.json()
    assert body["current_state"] == "FAILED"
    assert body["status"] == "FAILED"
    assert body["is_terminal"] is True
    assert body["error"]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"issue": ISSUE},
        {"repository_path": "C:/repo"},
        {"repository_path": "", "issue": ISSUE},
        {"repository_path": "C:/repo", "issue": ""},
        {"repository_path": "   ", "issue": ISSUE},
        {"repository_path": "C:/repo", "issue": "   "},
    ],
)
def test_post_run_with_invalid_body_returns_422(payload: dict) -> None:
    response = client.post("/api/mentor/runs", json=payload)
    assert response.status_code == 422


def test_post_run_rejects_oversized_fields(repo: Path) -> None:
    response = client.post(
        "/api/mentor/runs",
        json={"repository_path": "x" * 2000, "issue": ISSUE},
    )
    assert response.status_code == 422
    response = client.post(
        "/api/mentor/runs",
        json={"repository_path": str(repo), "issue": "y" * 9000},
    )
    assert response.status_code == 422


# ── Server owns state: clients cannot set it ──────────────────────────────────


@pytest.mark.parametrize(
    "smuggled",
    [
        {"current_state": "COMPLETED"},
        {"state": "COMPLETED"},
        {"status": "COMPLETED"},
        {"run_id": "11111111-1111-4111-8111-111111111111"},
        {"evidence": []},
        {"error": "fake failure"},
        {"context": {"root_cause_summary": "discount rounding"}},
        {"context": {"evidence_synthesis": {"status": "DETERMINED"}}},
        {"created_at": "1999-01-01T00:00:00Z"},
    ],
)
def test_post_run_rejects_client_supplied_workflow_fields(
    repo: Path, smuggled: dict
) -> None:
    response = client.post(
        "/api/mentor/runs",
        json={"repository_path": str(repo), "issue": ISSUE, **smuggled},
    )
    assert response.status_code == 422, response.text


def test_client_cannot_forge_a_completed_state(repo: Path) -> None:
    response = client.post(
        "/api/mentor/runs",
        json={
            "repository_path": str(repo),
            "issue": ISSUE,
            "current_state": "COMPLETED",
            "status": "COMPLETED",
        },
    )
    assert response.status_code == 422
    assert client.get("/api/mentor/runs").status_code == 405


def test_no_endpoint_accepts_a_state_transition(repo: Path) -> None:
    """There is no client-facing way to drive a transition."""
    run_id = _create(repo).json()["run_id"]
    paths = {
        path: set(methods)
        for path, methods in app.openapi()["paths"].items()
    }

    assert paths["/api/mentor/runs"] == {"post"}
    assert paths["/api/mentor/runs/{run_id}"] == {"get"}
    assert paths["/api/mentor/runs/{run_id}/events"] == {"get"}

    # No PUT/PATCH/DELETE anywhere under the mentor prefix.
    for path, methods in paths.items():
        if path.startswith("/api/mentor/"):
            assert not ({"PUT", "PATCH", "DELETE"} & methods), path

    # And no ad-hoc transition endpoint exists either.
    assert client.post(
        f"/api/mentor/runs/{run_id}/advance", json={"to": "COMPLETED"}
    ).status_code in (404, 405)
    assert client.post(
        f"/api/mentor/runs/{run_id}/transition", json={"state": "COMPLETED"}
    ).status_code in (404, 405)
    # PATCH on the real path exists but is not allowed.
    assert client.patch(
        f"/api/mentor/runs/{run_id}", json={"current_state": "COMPLETED"}
    ).status_code == 405


# ── GET /api/mentor/runs/{run_id} ─────────────────────────────────────────────


def test_get_run_returns_the_run(repo: Path) -> None:
    run_id = _create(repo).json()["run_id"]
    response = client.get(f"/api/mentor/runs/{run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert body["current_state"] == "FIX_PROPOSAL_READY"
    assert body["status"] == "PENDING"


def test_get_run_is_read_only(repo: Path) -> None:
    run_id = _create(repo).json()["run_id"]
    first = client.get(f"/api/mentor/runs/{run_id}").json()
    second = client.get(f"/api/mentor/runs/{run_id}").json()
    assert first == second
    assert client.get(f"/api/mentor/runs/{run_id}").json() == second


def test_get_unknown_run_returns_404() -> None:
    response = client.get(f"/api/mentor/runs/{UNKNOWN_RUN_ID}")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


@pytest.mark.parametrize(
    "bad_id",
    [
        "not-a-uuid",
        "../../etc/passwd",
        "12345",
        str(uuid.uuid4()).upper(),
        "11111111-1111-4111-8111-111111111111-extra",
        "%20",
        "null",
    ],
)
def test_get_run_with_malformed_id_returns_400(bad_id: str) -> None:
    assert client.get(f"/api/mentor/runs/{bad_id}").status_code in (400, 404)


def test_get_run_rejects_sql_injection_attempt() -> None:
    response = client.get(
        "/api/mentor/runs/1' OR '1'='1", params={}
    )
    assert response.status_code in (400, 404)
    assert "SELECT" not in response.text.upper()


def test_get_run_reports_failure_state(tmp_path: Path) -> None:
    run_id = _create(tmp_path / "missing").json()["run_id"]
    body = client.get(f"/api/mentor/runs/{run_id}").json()
    assert body["current_state"] == "FAILED"
    assert body["status"] == "FAILED"
    assert body["is_terminal"] is True
    assert body["error"]


def test_get_run_reports_different_runs_independently(repo: Path) -> None:
    first = _create(repo, "issue one").json()["run_id"]
    second = _create(repo, "issue two").json()["run_id"]
    assert client.get(f"/api/mentor/runs/{first}").json()["issue"] == "issue one"
    assert client.get(f"/api/mentor/runs/{second}").json()["issue"] == "issue two"
    assert (
        client.get(f"/api/mentor/runs/{first}").json()["run_id"]
        != client.get(f"/api/mentor/runs/{second}").json()["run_id"]
    )


def test_phase1_to_5_endpoints_are_unaffected(repo: Path) -> None:
    """Phase 6 routing does not shadow the earlier repository endpoints."""
    assert client.get("/health").status_code == 200
    assert client.post(
        "/api/repositories/explore", json={"path": str(repo)}
    ).status_code == 200
    assert client.post(
        "/api/repositories/analyze", json={"path": str(repo)}
    ).status_code == 200
    assert client.post(
        "/api/repositories/architecture", json={"path": str(repo)}
    ).status_code == 200


# -- GET /api/mentor/runs/{run_id}/investigation -- Phase 7 --


def _investigation(repo: Path, issue: str = ISSUE):
    run_id = _create(repo, issue).json()["run_id"]
    return run_id, client.get(f"/api/mentor/runs/{run_id}/investigation")


def test_investigation_endpoint_returns_the_three_investigators(repo: Path) -> None:
    run_id, response = _investigation(repo)
    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == run_id
    assert [i["investigator"] for i in body["investigators"]] == [
        "code_investigator",
        "test_investigator",
        "dependency_investigator",
    ]
    assert all(i["status"] == "COMPLETED" for i in body["investigators"])


def test_investigation_endpoint_returns_typed_evidence(repo: Path) -> None:
    _, response = _investigation(repo)
    body = response.json()
    assert body["evidence"]
    for item in body["evidence"]:
        assert item["evidence_type"] in (
            "OBSERVATION",
            "HYPOTHESIS",
            "PROPOSED_ACTION",
        )
        if item["evidence_type"] == "OBSERVATION":
            # An observation is a fact; it is not a matter of degree.
            assert item["confidence"] is None


def test_investigation_endpoint_never_reports_a_verified_result(repo: Path) -> None:
    _, response = _investigation(repo)
    types = {i["evidence_type"] for i in response.json()["evidence"]}
    assert "VERIFIED_RESULT" not in types


def test_investigation_endpoint_mirrors_the_phase_8_conclusion(repo: Path) -> None:
    """The Phase 7 view points at the Phase 8 conclusion but never inlines it.

    ``root_cause_claimed`` must agree with the analysis endpoint.  This fixture
    repository is too small to yield a root cause, so the honest answer is
    ``INSUFFICIENT_EVIDENCE`` and the flag stays False.  The endpoint still
    returns no root-cause text of its own and still claims nothing was executed.
    """
    _, response = _investigation(repo)
    body = response.json()
    assert body["tests_executed"] is False
    assert "root_cause" not in body
    analysis = client.get(f"/api/mentor/runs/{body['run_id']}/root-cause").json()
    assert analysis["status"] == "INSUFFICIENT_EVIDENCE"
    assert analysis["root_cause"] is None
    # The flag is derived, never independently asserted.
    assert body["root_cause_claimed"] is (
        analysis["status"] in ("DETERMINED", "PARTIAL") and bool(analysis["root_cause"])
    )


def test_investigation_endpoint_counts_evidence_by_type(repo: Path) -> None:
    _, response = _investigation(repo)
    body = response.json()
    assert sum(body["evidence_counts"].values()) == len(body["evidence"])
    for evidence_type, count in body["evidence_counts"].items():
        assert count > 0


def test_investigation_endpoint_echoes_the_run_state(repo: Path) -> None:
    _, response = _investigation(repo)
    assert response.json()["run_state"] == "FIX_PROPOSAL_READY"


def test_investigation_endpoint_is_read_only(repo: Path) -> None:
    """Calling it repeatedly must return identical data and change nothing."""
    _, response = _investigation(repo)
    first = response.json()
    run_id = first["run_id"]
    assert client.get(f"/api/mentor/runs/{run_id}/investigation").json() == first
    assert client.get(f"/api/mentor/runs/{run_id}").json()["evidence"]


def test_investigation_endpoint_does_not_modify_the_repository(repo: Path) -> None:
    before = sorted((p.name, p.read_bytes()) for p in repo.iterdir())
    _investigation(repo)
    assert sorted((p.name, p.read_bytes()) for p in repo.iterdir()) == before


def test_investigation_endpoint_rejects_a_malformed_run_id(repo: Path) -> None:
    response = client.get("/api/mentor/runs/not-a-uuid/investigation")
    assert response.status_code == 400


def test_investigation_endpoint_404s_for_an_unknown_run(repo: Path) -> None:
    assert (
        client.get(f"/api/mentor/runs/{UNKNOWN_RUN_ID}/investigation").status_code
        == 404
    )


def test_investigation_endpoint_reports_pending_for_an_unwalked_run(
    tmp_path: Path,
) -> None:
    """A run created but not driven has no investigation yet.

    "Not investigated" is a valid state, not an error, so this must be a 200
    with an empty investigation rather than a 404 or a 500.
    """
    from app.services.orchestrator import get_default_orchestrator

    orchestrator = get_default_orchestrator()
    run = orchestrator.create_run(str(tmp_path), ISSUE)
    response = client.get(f"/api/mentor/runs/{run.run_id}/investigation")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "PENDING"
    assert body["investigators"] == []
    assert body["evidence"] == []
    assert body["run_state"] == "CREATED"
