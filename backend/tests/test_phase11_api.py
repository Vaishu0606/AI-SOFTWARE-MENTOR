"""
Phase 11 REST API tests — the approval gate and its two read endpoints.

``POST /api/mentor/runs/{run_id}/approve-fix``
``GET  /api/mentor/runs/{run_id}/fix-application``
``GET  /api/mentor/runs/{run_id}/verification``

What this file is for
---------------------
The gate is the only place in the system where a request turns into a write to
somebody's source tree, so most of these tests are about refusal: that a run in
the wrong state cannot be approved, that a diff the reviewer never saw cannot be
approved by sending the right-looking fields, and that a request which tries to
carry its own patch is rejected rather than quietly obeyed.

The tests that matter most are the two that run the *same* repository with two
different test suites.  ``repo`` has a suite that exercises the defect, so the run
verifies the fix.  ``blind_repo`` has a suite that divides evenly and is green on
the broken code, so the same approval produces ``PASSED`` with
``defect_demonstrated=false``.  Only the second one distinguishes a system that
verified something from a system that reported a green suite.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models.fix_application import FixApplicationStatus
from app.models.verification import VerificationStatus
from app.models.workflow import WorkflowEventType, WorkflowState
from app.services.orchestrator import MentorOrchestrator

client = TestClient(app)

ISSUE = "Checkout is failing when a discount code is applied."
UNKNOWN_RUN_ID = "00000000-0000-4000-8000-000000000000"
BUGGY = "    return int(subtotal_cents / 100) * code.value"
FIXED = "    return int(subtotal_cents * code.value / 100)"


def _build_repo(tmp_path: Path, name: str, assertion: str) -> Path:
    """A one-file discount service plus one test asserting ``assertion``.

    The two callers differ only in the assertion, which is the point: the defect,
    the proposal, the diff and the application are byte-for-byte identical, so
    whatever differs in the response is down to whether the suite could tell the
    broken code from the fixed code.
    """
    root = tmp_path / name
    (root / "app" / "services").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "main.py").write_text(
        "from app.services.discount_service import calculate_discount_cents\n\n\n"
        "def run():\n    return calculate_discount_cents\n",
        encoding="utf-8",
    )
    (root / "app" / "services" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "services" / "discount_service.py").write_text(
        "def calculate_discount_cents(subtotal_cents, code):\n"
        '    """Return the discount in cents for ``code`` percent off."""\n'
        f"{BUGGY}\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_discount.py").write_text(
        "from app.services.discount_service import calculate_discount_cents\n"
        "\n"
        "\n"
        "class Code:\n"
        "    def __init__(self, value):\n"
        "        self.value = value\n"
        "\n"
        "\n"
        "def test_discount():\n"
        f"    {assertion}\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A suite that exercises the defect, so verification can demonstrate it."""
    return _build_repo(
        tmp_path,
        "ApiApp",
        "# 10% of 1050 is 105; truncating the subtotal first gives 100.\n"
        "    assert calculate_discount_cents(1050, Code(10)) == 105",
    )


@pytest.fixture
def blind_repo(tmp_path: Path) -> Path:
    """A suite that is green on the broken code, so it can demonstrate nothing."""
    return _build_repo(
        tmp_path,
        "BlindApiApp",
        "# 1000 divides evenly, so this passes before and after the fix.\n"
        "    assert calculate_discount_cents(1000, Code(10)) == 100",
    )


def _create(repo_path: Path, issue: str = ISSUE) -> dict:
    response = client.post(
        "/api/mentor/runs",
        json={"repository_path": str(repo_path), "issue": issue},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _stalled_run(repo_path: Path) -> str:
    """Create a run and drive it to the gate.  Returns the run id."""
    return _create(repo_path)["run_id"]


def _approve(run_id: str, **body: object):
    payload: dict[str, object] = {"approved_by": "reviewer"}
    payload.update(body)
    return client.post(f"/api/mentor/runs/{run_id}/approve-fix", json=payload)


def _get_application(run_id: str):
    return client.get(f"/api/mentor/runs/{run_id}/fix-application")


def _get_verification(run_id: str):
    return client.get(f"/api/mentor/runs/{run_id}/verification")


def _malformed_run_id() -> str:
    return "not-a-uuid"


# ── The gate: happy path ──────────────────────────────────────────────────────


def test_approving_a_proposal_applies_it_and_verifies_it(repo: Path) -> None:
    """The whole Phase 11 walk over HTTP, and every claim in the response.

    One request carries a run from ``FIX_PROPOSAL_READY`` to ``COMPLETED`` and
    reports all three records — approval, application, verification — so a client
    that never opened an SSE stream still gets the whole story.
    """
    run_id = _stalled_run(repo)

    response = _approve(run_id, comment="the arithmetic truncates the subtotal")

    assert response.status_code == 200, response.text
    body = response.json()

    # The run completed, which requires a real exit code 0 — not just "no stages
    # left".  COMPLETED is unreachable without a recorded verification result.
    assert body["run"]["current_state"] == WorkflowState.COMPLETED.value
    assert body["run"]["status"] == "COMPLETED"

    approval = body["approval"]
    assert approval["approved"] is True
    assert approval["approved_by"] == "reviewer"
    assert approval["comment"] == "the arithmetic truncates the subtotal"
    assert approval["proposal_fingerprint"].startswith("sha256:")
    assert approval["affected_files"] == ["app/services/discount_service.py"]
    assert approval["change_count"] >= 1

    application = body["application"]
    assert application["status"] == FixApplicationStatus.APPLIED.value
    assert application["verified"] is True
    assert application["repository_modified"] is True
    assert application["rolled_back"] is False
    assert [f["file_path"] for f in application["files"]] == [
        "app/services/discount_service.py"
    ]
    assert application["proposal_fingerprint"] == approval["proposal_fingerprint"]

    verification = body["verification"]
    assert verification["status"] == VerificationStatus.PASSED.value
    assert verification["tests_executed"] is True
    assert verification["regression_check"] is True
    assert verification["fix_applied"] is True
    assert verification["test_run"]["exit_code"] == 0
    assert verification["test_run"]["started"] is True
    assert verification["test_run"]["timed_out"] is False
    # And the repository really holds the fix now.
    on_disk = (repo / "app" / "services" / "discount_service.py").read_text(
        encoding="utf-8"
    )
    assert FIXED in on_disk
    assert BUGGY not in on_disk


def test_the_response_carries_the_workflow_events(repo: Path) -> None:
    """A client with no SSE connection still sees the workflow signal.

    The gate closes before it opens: nothing may be written before the approval,
    and the event order is the only place that ordering is visible.
    """
    run_id = _stalled_run(repo)
    body = _approve(run_id).json()
    types = [event["event_type"] for event in body["events"]]

    for expected in (
        WorkflowEventType.FIX_APPROVED,
        WorkflowEventType.FIX_APPLICATION_STARTED,
        WorkflowEventType.FIX_APPLIED,
        WorkflowEventType.TEST_PLAN_STARTED,
        WorkflowEventType.VERIFICATION_STARTED,
        WorkflowEventType.VERIFICATION_COMPLETED,
    ):
        assert expected.value in types, f"{expected.value} missing from {types}"
    assert types.index(WorkflowEventType.FIX_APPROVED.value) < types.index(
        WorkflowEventType.FIX_APPLIED.value
    )


def test_a_fingerprint_the_client_displayed_is_accepted(repo: Path) -> None:
    """The compare-and-swap guard works when the client got it right."""
    run_id = _stalled_run(repo)
    fingerprint = client.get(
        f"/api/mentor/runs/{run_id}/fix-proposal"
    ).json()["proposal_fingerprint"]

    response = _approve(run_id, expected_fingerprint=fingerprint)

    assert response.status_code == 200, response.text
    assert response.json()["application"]["status"] == (
        FixApplicationStatus.APPLIED.value
    )


# ── The gate: refusals ────────────────────────────────────────────────────────


def test_a_stale_fingerprint_is_refused_and_nothing_is_written(repo: Path) -> None:
    """The load-bearing refusal: a reviewer must approve *this* diff.

    The fingerprint is well-formed and wrong, so it survives the request-model
    shape check and is caught by the comparison against the stored proposal.  The
    repository must be untouched, and the run must still be waiting at the gate —
    a refusal is not a partial approval.
    """
    run_id = _stalled_run(repo)
    before = (repo / "app" / "services" / "discount_service.py").read_text(
        encoding="utf-8"
    )

    response = _approve(
        run_id, expected_fingerprint="sha256:" + "0" * 64
    )

    assert response.status_code == 409
    assert "no longer the one on record" in response.json()["detail"]
    assert (
        repo / "app" / "services" / "discount_service.py"
    ).read_text(encoding="utf-8") == before

    run = client.get(f"/api/mentor/runs/{run_id}").json()
    assert run["current_state"] == WorkflowState.FIX_PROPOSAL_READY.value


def test_approving_a_run_with_no_reviewable_proposal_is_refused(
    tmp_path: Path,
) -> None:
    """An honest negative proposal has no diff, so there is nothing to approve.

    ``clean_repo`` has no defect for Phase 8 to diagnose, so its proposal is
    ``INSUFFICIENT_EVIDENCE``.  The gate must refuse it — and must say *which*
    reason, because "we could not find a bug" and "you already approved this" are
    both 409s and only one of them means re-running the investigation.
    """
    clean = _build_repo(
        tmp_path,
        "CleanApiApp",
        "assert calculate_discount_cents(1000, Code(10)) == 100",
    )
    (clean / "app" / "services" / "discount_service.py").write_text(
        "def calculate_discount_cents(subtotal_cents, code):\n"
        '    """Return the discount in cents."""\n'
        "    return 0\n",
        encoding="utf-8",
    )
    run_id = _stalled_run(clean)

    response = _approve(run_id)

    assert response.status_code == 409
    assert "no diff to review" in response.json()["detail"]


def test_approving_twice_is_refused_and_applies_nothing_new(repo: Path) -> None:
    """The gate is one-way; a second approval never reaches the applier.

    The application record is compared before and after rather than only its
    status, because ``APPLIED`` twice is the failure this guards against and a
    status check alone would not notice a second write that reported the same
    status.
    """
    run_id = _stalled_run(repo)
    assert _approve(run_id).status_code == 200
    before = _get_application(run_id).json()
    on_disk_before = (repo / "app" / "services" / "discount_service.py").read_text(
        encoding="utf-8"
    )

    second = _approve(run_id)

    assert second.status_code == 409
    assert _get_application(run_id).json() == before
    assert (
        repo / "app" / "services" / "discount_service.py"
    ).read_text(encoding="utf-8") == on_disk_before


def test_a_cancelled_run_cannot_be_approved(repo: Path) -> None:
    """Cancelling is a refusal the run itself records, and it outranks the gate."""
    run_id = _stalled_run(repo)
    MentorOrchestrator().cancel_run(run_id)

    response = _approve(run_id)

    assert response.status_code == 409


# ── The gate: the request cannot carry a patch ────────────────────────────────


def test_a_body_carrying_a_diff_is_rejected(repo: Path) -> None:
    """``extra="forbid"`` turns an attempt to smuggle a patch into a 422.

    The important part is that it is an *error* rather than a silently dropped
    field: a client that posts a diff and gets a 200 would reasonably believe its
    diff was applied.
    """
    run_id = _stalled_run(repo)

    response = _approve(
        run_id,
        diff="--- a/app/x.py\n+++ b/app/x.py\n",
        file_path="app/services/discount_service.py",
        line=3,
    )

    assert response.status_code == 422
    on_disk = (repo / "app" / "services" / "discount_service.py").read_text(
        encoding="utf-8"
    )
    assert BUGGY in on_disk


def test_a_body_trying_to_set_the_state_is_rejected(repo: Path) -> None:
    """A client cannot walk itself through the gate by naming a state."""
    run_id = _stalled_run(repo)

    response = _approve(run_id, current_state="VERIFIED", status="COMPLETED")

    assert response.status_code == 422
    assert client.get(f"/api/mentor/runs/{run_id}").json()["current_state"] == (
        WorkflowState.FIX_PROPOSAL_READY.value
    )


def test_approved_false_is_rejected(repo: Path) -> None:
    """A refusal is a 409, never a stored approval that says no.

    ``approved`` is ``Literal[True]``, so the type refuses ``false`` before the
    request reaches the orchestrator.  A stored ``approved=false`` would be a
    permanent veto that later phases have to learn to honour, which is how a gate
    acquires a bypass.
    """
    run_id = _stalled_run(repo)

    response = _approve(run_id, approved=False)

    assert response.status_code == 422


def test_an_empty_reviewer_is_rejected(repo: Path) -> None:
    """An approval nobody can attribute is not a review."""
    run_id = _stalled_run(repo)

    for bad in ("", "   "):
        response = _approve(run_id, approved_by=bad)
        assert response.status_code == 422, bad


def test_a_malformed_fingerprint_is_rejected_before_it_is_compared(
    repo: Path,
) -> None:
    """Nonsense is a 422; only a well-formed mismatch is a 409.

    The distinction matters to a client: 422 means "fix your request", 409 means
    "re-read the diff and ask again".
    """
    run_id = _stalled_run(repo)

    response = _approve(run_id, expected_fingerprint="not-a-digest")

    assert response.status_code == 422


def test_an_oversized_comment_is_rejected(repo: Path) -> None:
    """Bounded free text, because it is stored and echoed back."""
    run_id = _stalled_run(repo)

    response = _approve(run_id, comment="x" * 4001)

    assert response.status_code == 422


# ── The gate: run-id and lookup handling ──────────────────────────────────────


def test_an_unknown_run_is_404(repo: Path) -> None:
    """Well-formed but unknown is 404, not 409."""
    response = _approve(UNKNOWN_RUN_ID)

    assert response.status_code == 404


def test_a_malformed_run_id_is_400(repo: Path) -> None:
    """Rejected before it reaches persistence."""
    response = _approve(_malformed_run_id())

    assert response.status_code == 400


@pytest.mark.parametrize(
    "method,path_template",
    [
        ("post", "/api/mentor/runs/{}/approve-fix"),
        ("get", "/api/mentor/runs/{}/fix-application"),
        ("get", "/api/mentor/runs/{}/verification"),
    ],
)
def test_every_phase11_route_maps_a_malformed_id_to_400(
    method: str, path_template: str
) -> None:
    """The three new routes agree on run-id handling.

    Paramed over the routes rather than written three times so a fourth Phase 11
    route cannot quietly disagree about 400 versus 404.
    """
    path = path_template.format(_malformed_run_id())
    response = (
        client.post(path, json={"approved_by": "reviewer"})
        if method == "post"
        else client.get(path)
    )

    assert response.status_code == 400


@pytest.mark.parametrize(
    "method,path_template",
    [
        ("post", "/api/mentor/runs/{}/approve-fix"),
        ("get", "/api/mentor/runs/{}/fix-application"),
        ("get", "/api/mentor/runs/{}/verification"),
    ],
)
def test_every_phase11_route_maps_an_unknown_id_to_404(
    method: str, path_template: str
) -> None:
    """Same reason, for the lookup failure."""
    unknown = str(uuid.uuid4())
    path = path_template.format(unknown)
    response = (
        client.post(path, json={"approved_by": "reviewer"})
        if method == "post"
        else client.get(path)
    )

    assert response.status_code == 404


# ── Read endpoints before approval ────────────────────────────────────────────


def test_reads_are_409_before_the_gate_is_passed(repo: Path) -> None:
    """Neither record exists before approval, and that is a conflict, not a null.

    A 200 with empty fields would let a client render "applied nothing" and
    "verified nothing" as though they were answers; they are the absence of an
    answer.
    """
    run_id = _stalled_run(repo)

    application = _get_application(run_id)
    verification = _get_verification(run_id)

    assert application.status_code == 409
    assert verification.status_code == 409
    assert WorkflowState.FIX_PROPOSAL_READY.value in application.json()["detail"]
    assert WorkflowState.FIX_PROPOSAL_READY.value in verification.json()["detail"]


# ── The honesty case: a suite that cannot see the defect ──────────────────────


def test_a_green_suite_that_ignores_the_defect_reports_false(blind_repo: Path) -> None:
    """The same approval, on a suite that divides evenly, demonstrates nothing.

    Everything else here is identical to the happy path: same defect, same
    proposal, same diff, same application, ``PASSED``, exit code 0, no regression.
    The one field that changes is ``defect_demonstrated``, and it changes because
    the suite was re-run against the reverted change and passed there too.

    This is the test that would fail if the flag were derived from the test
    *plan* — the plan asks for a discriminating scenario either way — or read off
    the test source, or simply hard-wired true.
    """
    run_id = _stalled_run(blind_repo)

    body = _approve(run_id).json()

    assert body["run"]["current_state"] == WorkflowState.COMPLETED.value
    assert body["verification"]["status"] == VerificationStatus.PASSED.value
    assert body["verification"]["test_run"]["exit_code"] == 0
    assert body["verification"]["regression_check"] is True
    assert body["verification"]["defect_demonstrated"] is False
    assert any(
        "also pass against the tree with the change reverted" in finding
        for finding in body["verification"]["findings"]
    ), "the refusal must say why"


# ── Read endpoints after approval ─────────────────────────────────────────────


def test_the_application_read_matches_what_approval_reported(repo: Path) -> None:
    """The GET and the POST tell the same story.

    Two code paths, one stored record; if they could disagree a client that
    refreshes after approving would see the change vanish.
    """
    run_id = _stalled_run(repo)
    approved = _approve(run_id).json()["application"]

    fetched = _get_application(run_id).json()

    assert fetched["status"] == approved["status"]
    assert fetched["verified"] == approved["verified"]
    assert fetched["repository_modified"] == approved["repository_modified"]
    assert fetched["files"] == approved["files"]
    assert fetched["proposal_fingerprint"] == approved["proposal_fingerprint"]


def test_the_verification_read_matches_what_approval_reported(repo: Path) -> None:
    """Same guarantee for the verification record, claim booleans included."""
    run_id = _stalled_run(repo)
    approved = _approve(run_id).json()["verification"]

    fetched = _get_verification(run_id).json()

    for field in (
        "status",
        "tests_executed",
        "regression_check",
        "defect_demonstrated",
        "fix_applied",
        "targets",
    ):
        assert fetched[field] == approved[field], field
    assert fetched["test_run"] == approved["test_run"]


def test_the_read_endpoints_are_idempotent(repo: Path) -> None:
    """Calling them twice changes nothing, including the repository."""
    run_id = _stalled_run(repo)
    _approve(run_id)

    first_application = _get_application(run_id).json()
    first_verification = _get_verification(run_id).json()
    second_application = _get_application(run_id).json()
    second_verification = _get_verification(run_id).json()

    assert first_application == second_application
    assert first_verification == second_verification
    # repository_modified stays true because the write happened during approval;
    # the reads did not write anything.
    assert second_application["repository_modified"] is True


def test_the_verification_response_keeps_the_four_claims_separate(
    repo: Path,
) -> None:
    """Four booleans, four claims, none of them summarised into a verdict.

    A client can collapse them; the server must not do it for them.
    """
    run_id = _stalled_run(repo)
    _approve(run_id)

    body = _get_verification(run_id).json()

    assert set(body) >= {
        "tests_executed",
        "regression_check",
        "defect_demonstrated",
        "fix_applied",
        "limitations",
        "findings",
    }
    assert body["limitations"], "every result states what it does not establish"
    assert body["verified_file_hashes"], "the result is bound to tested bytes"
    # The recorded hash is the file now on disk, not the one that was proposed.
    assert FIXED in (
        repo / "app" / "services" / "discount_service.py"
    ).read_text(encoding="utf-8")


def test_the_application_response_never_carries_source_text(repo: Path) -> None:
    """Digests, not a diff — so the record can be logged and streamed freely."""
    run_id = _stalled_run(repo)
    _approve(run_id)

    body = _get_application(run_id).json()

    serialised = str(body)
    # No source, and no diff markers either.  The "+" character is not asserted
    # against: the ISO timestamp legitimately contains "+00:00", and a test that
    # failed on that would be testing the clock rather than the redaction.
    assert "subtotal_cents" not in serialised
    assert "subtotal_cents * code.value" not in serialised
    assert "--- a/" not in serialised
    assert "+++ b/" not in serialised
    assert "@@" not in serialised
    for entry in body["files"]:
        assert set(entry) == {
            "file_path",
            "before_sha256",
            "after_sha256",
            "lines_applied",
            "modified",
        }
        assert len(entry["before_sha256"]) == 64
        assert len(entry["after_sha256"]) == 64
