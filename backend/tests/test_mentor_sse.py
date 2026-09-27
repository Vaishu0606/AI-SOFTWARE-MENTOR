"""
Phase 6 tests — Server-Sent Events stream.

GET /api/mentor/runs/{run_id}/events must:
- respond with ``text/event-stream``
- stream only the requested run's events
- respect ``max_events`` / ``timeout`` so a stream always terminates
- support resuming with ``last_event_id`` / ``Last-Event-ID``
- return 404 for an unknown run and 400 for a malformed run ID
"""

from __future__ import annotations

import json
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
    root = tmp_path / "SseRepo"
    root.mkdir()
    (root / "main.py").write_text(
        "from fastapi import FastAPI\napp = FastAPI()\n", encoding="utf-8"
    )
    return root


def _create_run(repo: Path, issue: str = ISSUE) -> str:
    response = client.post(
        "/api/mentor/runs", json={"repository_path": str(repo), "issue": issue}
    )
    assert response.status_code == 201, response.text
    return response.json()["run_id"]


def _parse_sse(text: str) -> list[dict]:
    """Parse an SSE body into a list of {id, event, data} frames.

    Comment frames (``: keep-alive``) are ignored.
    """
    frames: list[dict] = []
    for block in text.split("\n\n"):
        block = block.strip("\n")
        if not block or block.startswith(":"):
            continue
        frame: dict = {}
        for line in block.splitlines():
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if field == "data":
                frame["data"] = json.loads(value)
            else:
                frame[field] = value
        if frame:
            frames.append(frame)
    return frames


# ── Content type and framing ──────────────────────────────────────────────────


def test_sse_content_type(repo: Path) -> None:
    run_id = _create_run(repo)
    response = client.get(
        f"/api/mentor/runs/{run_id}/events", params={"max_events": 1}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")


def test_sse_sets_no_cache_headers(repo: Path) -> None:
    run_id = _create_run(repo)
    response = client.get(
        f"/api/mentor/runs/{run_id}/events", params={"max_events": 1}
    )
    assert "no-cache" in response.headers["cache-control"]
    assert response.headers["x-accel-buffering"] == "no"


def test_sse_starts_with_a_comment_frame(repo: Path) -> None:
    run_id = _create_run(repo)
    response = client.get(
        f"/api/mentor/runs/{run_id}/events", params={"max_events": 1}
    )
    assert response.text.startswith(":")


def test_sse_frames_carry_id_event_and_data(repo: Path) -> None:
    run_id = _create_run(repo)
    response = client.get(
        f"/api/mentor/runs/{run_id}/events", params={"max_events": 2}
    )
    frames = _parse_sse(response.text)
    assert len(frames) == 2
    for frame in frames:
        assert uuid.UUID(frame["id"])
        assert frame["event"] in {
            "RUN_CREATED",
            "STATE_CHANGED",
            "STAGE_STARTED",
            "STAGE_COMPLETED",
            "STAGE_PENDING",
            "STAGE_FAILED",
            "RUN_COMPLETED",
            "RUN_FAILED",
            "RUN_CANCELLED",
        }
        assert frame["data"]["run_id"] == run_id


def test_sse_data_contains_the_full_event(repo: Path) -> None:
    run_id = _create_run(repo)
    response = client.get(
        f"/api/mentor/runs/{run_id}/events", params={"max_events": 1}
    )
    payload = _parse_sse(response.text)[0]["data"]
    assert set(payload) == {
        "event_id",
        "run_id",
        "sequence",
        "event_type",
        "state",
        "status",
        "message",
        "timestamp",
        "payload",
    }
    assert payload["sequence"] == 1
    assert payload["event_type"] == "RUN_CREATED"
    assert payload["state"] == "CREATED"
    assert payload["status"] == "PENDING"


def test_sse_frames_are_ordered_by_sequence(repo: Path) -> None:
    run_id = _create_run(repo)
    response = client.get(f"/api/mentor/runs/{run_id}/events", params={"timeout": 0.5})
    frames = _parse_sse(response.text)
    sequences = [f["data"]["sequence"] for f in frames]
    assert sequences == sorted(sequences)
    assert sequences == list(range(1, len(sequences) + 1))


def test_sse_replays_the_whole_event_log(repo: Path) -> None:
    run_id = _create_run(repo)
    run = client.get(f"/api/mentor/runs/{run_id}").json()
    response = client.get(f"/api/mentor/runs/{run_id}/events", params={"timeout": 0.5})
    frames = _parse_sse(response.text)
    assert len(frames) == run["event_count"]
    assert frames[0]["event"] == "RUN_CREATED"
    assert frames[-1]["event"] == "STATE_CHANGED"


# ── Termination ───────────────────────────────────────────────────────────────


def test_max_events_bounds_the_stream(repo: Path) -> None:
    run_id = _create_run(repo)
    response = client.get(
        f"/api/mentor/runs/{run_id}/events", params={"max_events": 3}
    )
    assert len(_parse_sse(response.text)) == 3


def test_max_events_of_one_returns_a_single_event(repo: Path) -> None:
    run_id = _create_run(repo)
    response = client.get(
        f"/api/mentor/runs/{run_id}/events", params={"max_events": 1}
    )
    frames = _parse_sse(response.text)
    assert len(frames) == 1
    assert frames[0]["data"]["sequence"] == 1


def test_timeout_closes_the_stream_for_a_waiting_run(repo: Path) -> None:
    run_id = _create_run(repo)
    response = client.get(
        f"/api/mentor/runs/{run_id}/events", params={"timeout": 0.2}
    )
    assert response.status_code == 200
    assert _parse_sse(response.text)  # the backlog was flushed before closing


@pytest.mark.parametrize("bad", [0, -1, 5000])
def test_invalid_timeout_returns_422(repo: Path, bad: int) -> None:
    run_id = _create_run(repo)
    response = client.get(f"/api/mentor/runs/{run_id}/events", params={"timeout": bad})
    assert response.status_code == 422


@pytest.mark.parametrize("bad", [0, -1, 100000])
def test_invalid_max_events_returns_422(repo: Path, bad: int) -> None:
    run_id = _create_run(repo)
    response = client.get(
        f"/api/mentor/runs/{run_id}/events", params={"max_events": bad}
    )
    assert response.status_code == 422


def test_stream_closes_immediately_for_a_terminal_run(repo: Path, tmp_path: Path) -> None:
    run_id = _create_run(tmp_path / "missing")
    run = client.get(f"/api/mentor/runs/{run_id}").json()
    assert run["is_terminal"] is True
    response = client.get(f"/api/mentor/runs/{run_id}/events", params={"timeout": 30})
    frames = _parse_sse(response.text)
    assert len(frames) == run["event_count"]
    assert frames[-1]["event"] == "RUN_FAILED"


# ── Resume ────────────────────────────────────────────────────────────────────


def test_last_event_id_query_resumes_after_that_event(repo: Path) -> None:
    run_id = _create_run(repo)
    all_frames = _parse_sse(
        client.get(f"/api/mentor/runs/{run_id}/events", params={"timeout": 0.5}).text
    )
    cursor = all_frames[2]["id"]

    resumed = _parse_sse(
        client.get(
            f"/api/mentor/runs/{run_id}/events",
            params={"last_event_id": cursor, "timeout": 0.5},
        ).text
    )
    assert [f["data"]["sequence"] for f in resumed] == [
        f["data"]["sequence"] for f in all_frames[3:]
    ]


def test_last_event_id_header_resumes_after_that_event(repo: Path) -> None:
    run_id = _create_run(repo)
    all_frames = _parse_sse(
        client.get(f"/api/mentor/runs/{run_id}/events", params={"timeout": 0.5}).text
    )
    cursor = all_frames[0]["id"]

    response = client.get(
        f"/api/mentor/runs/{run_id}/events",
        params={"timeout": 0.5},
        headers={"Last-Event-ID": cursor},
    )
    assert [
        f["data"]["sequence"] for f in _parse_sse(response.text)
    ] == [f["data"]["sequence"] for f in all_frames[1:]]


def test_unknown_resume_cursor_replays_everything(repo: Path) -> None:
    run_id = _create_run(repo)
    total = client.get(f"/api/mentor/runs/{run_id}").json()["event_count"]
    frames = _parse_sse(
        client.get(
            f"/api/mentor/runs/{run_id}/events",
            params={"last_event_id": "not-a-real-event-id", "timeout": 0.5},
        ).text
    )
    assert len(frames) == total


# ── Run isolation ─────────────────────────────────────────────────────────────


def test_sse_never_leaks_another_runs_events(repo: Path) -> None:
    first = _create_run(repo, "issue one")
    second = _create_run(repo, "issue two")

    first_frames = _parse_sse(
        client.get(f"/api/mentor/runs/{first}/events", params={"timeout": 0.5}).text
    )
    second_frames = _parse_sse(
        client.get(f"/api/mentor/runs/{second}/events", params={"timeout": 0.5}).text
    )

    assert first_frames and second_frames
    assert all(f["data"]["run_id"] == first for f in first_frames)
    assert all(f["data"]["run_id"] == second for f in second_frames)
    assert {f["id"] for f in first_frames}.isdisjoint(
        {f["id"] for f in second_frames}
    )


def test_failed_run_stream_only_has_its_own_events(repo: Path, tmp_path: Path) -> None:
    ok_run = _create_run(repo)
    bad_run = _create_run(tmp_path / "missing")
    bad_frames = _parse_sse(
        client.get(f"/api/mentor/runs/{bad_run}/events", params={"timeout": 0.5}).text
    )
    assert all(f["data"]["run_id"] == bad_run for f in bad_frames)
    assert "RUN_FAILED" in {f["event"] for f in bad_frames}
    assert "RUN_FAILED" not in {
        f["event"]
        for f in _parse_sse(
            client.get(f"/api/mentor/runs/{ok_run}/events", params={"timeout": 0.5}).text
        )
    }


def test_cursor_from_another_run_is_ignored(repo: Path) -> None:
    first = _create_run(repo, "issue one")
    second = _create_run(repo, "issue two")
    foreign = _parse_sse(
        client.get(f"/api/mentor/runs/{first}/events", params={"timeout": 0.5}).text
    )[0]["id"]

    frames = _parse_sse(
        client.get(
            f"/api/mentor/runs/{second}/events",
            params={"last_event_id": foreign, "timeout": 0.5},
        ).text
    )
    assert all(f["data"]["run_id"] == second for f in frames)
    assert frames[0]["data"]["sequence"] == 1


# ── Error handling ────────────────────────────────────────────────────────────


def test_unknown_run_returns_404_not_a_stream() -> None:
    response = client.get(
        f"/api/mentor/runs/{UNKNOWN_RUN_ID}/events", params={"max_events": 1}
    )
    assert response.status_code == 404
    assert "text/event-stream" not in response.headers.get("content-type", "")


@pytest.mark.parametrize("bad_id", ["nope", "..", "1' OR 1=1 --", "%2e%2e%2f"])
def test_malformed_run_id_returns_400(bad_id: str) -> None:
    response = client.get(f"/api/mentor/runs/{bad_id}/events", params={"max_events": 1})
    assert response.status_code in (400, 404)
    assert "text/event-stream" not in response.headers.get("content-type", "")


def test_uppercase_uuid_is_rejected(repo: Path) -> None:
    run_id = _create_run(repo)
    response = client.get(
        f"/api/mentor/runs/{run_id.upper()}/events", params={"max_events": 1}
    )
    assert response.status_code == 400


def test_stream_survives_a_short_client_disconnect(repo: Path) -> None:
    """Closing the response early must not raise or corrupt the run."""
    run_id = _create_run(repo)
    with client.stream(
        "GET", f"/api/mentor/runs/{run_id}/events", params={"max_events": 2}
    ) as response:
        assert response.status_code == 200
        next(response.iter_text(), None)
    # The run is untouched and its history is still complete.  Phase 7 replaced
    # the investigation placeholder's two events with nine real ones and added a
    # fourth stage pair, taking the Phase 6 walk's ten events to 21.  Phase 8
    # turned two more placeholders into real stages -- one emitting three events
    # instead of two -- which takes the walk to 29 up to ROOT_CAUSE_READY.  Phase
    # 9 turns that state's placeholder into a real gate (2 -> 3 events), adds the
    # proposal stage (5 events including its two progress reports) and adds the
    # approval-gate placeholder (3 events), taking the walk to 39.
    run = client.get(f"/api/mentor/runs/{run_id}").json()
    assert run["current_state"] == "FIX_PROPOSAL_READY"
    assert run["event_count"] == 39
