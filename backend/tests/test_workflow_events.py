"""
Phase 6 tests — workflow event store.

Covers append-only behaviour, per-run ordering, run scoping, and the
guarantees the SSE endpoint depends on.
"""

from __future__ import annotations

import pytest

from app.models.workflow import (
    RunStatus,
    WorkflowEventType,
    WorkflowState,
    new_event_id,
)
from app.services.event_store import EventStore


@pytest.fixture
def store() -> EventStore:
    return EventStore()


def _append(store: EventStore, run_id: str, message: str = "step"):
    return store.append(
        run_id=run_id,
        event_type=WorkflowEventType.STATE_CHANGED,
        state=WorkflowState.CREATED,
        status=RunStatus.RUNNING,
        message=message,
        payload={"message": message},
    )


# ── Creation ──────────────────────────────────────────────────────────────────


def test_append_returns_a_persisted_event(store: EventStore) -> None:
    event = _append(store, "run-a", "first")
    assert event.event_id
    assert event.run_id == "run-a"
    assert event.sequence == 1
    assert event.event_type is WorkflowEventType.STATE_CHANGED
    assert event.state is WorkflowState.CREATED
    assert event.status is RunStatus.RUNNING
    assert event.message == "first"
    assert event.payload == {"message": "first"}
    assert event.timestamp is not None


def test_appended_event_is_readable_from_the_store(store: EventStore) -> None:
    written = _append(store, "run-a", "first")
    read_back = store.list("run-a")
    assert len(read_back) == 1
    assert read_back[0].event_id == written.event_id
    assert read_back[0].sequence == written.sequence
    assert read_back[0].message == written.message


def test_empty_run_has_no_events(store: EventStore) -> None:
    assert store.list("run-empty") == []
    assert store.count("run-empty") == 0
    assert store.latest_sequence("run-empty") == 0


# ── Uniqueness and ordering ───────────────────────────────────────────────────


def test_event_ids_are_unique(store: EventStore) -> None:
    ids = {_append(store, "run-a").event_id for _ in range(25)}
    assert len(ids) == 25


def test_sequences_are_gap_free_and_ascending(store: EventStore) -> None:
    for i in range(10):
        _append(store, "run-a", f"step {i}")
    events = store.list("run-a")
    assert [e.sequence for e in events] == list(range(1, 11))
    assert [e.message for e in events] == [f"step {i}" for i in range(10)]


def test_after_sequence_filters_earlier_events(store: EventStore) -> None:
    for i in range(5):
        _append(store, "run-a", f"step {i}")
    tail = store.list("run-a", after_sequence=3)
    assert [e.sequence for e in tail] == [4, 5]
    assert store.list("run-a", after_sequence=5) == []


def test_limit_bounds_the_page(store: EventStore) -> None:
    for i in range(10):
        _append(store, "run-a", f"step {i}")
    page = store.list("run-a", limit=4)
    assert [e.sequence for e in page] == [1, 2, 3, 4]
    assert store.count("run-a") == 10


def test_count_and_latest_sequence(store: EventStore) -> None:
    for i in range(7):
        _append(store, "run-a", f"step {i}")
    assert store.count("run-a") == 7
    assert store.latest_sequence("run-a") == 7


# ── Run scoping ───────────────────────────────────────────────────────────────


def test_events_belong_to_the_correct_run(store: EventStore) -> None:
    a = _append(store, "run-a", "a1")
    b = _append(store, "run-b", "b1")

    assert [e.event_id for e in store.list("run-a")] == [a.event_id]
    assert [e.event_id for e in store.list("run-b")] == [b.event_id]
    assert all(e.run_id == "run-a" for e in store.list("run-a"))


def test_sequences_are_per_run(store: EventStore) -> None:
    _append(store, "run-a")
    _append(store, "run-b")
    _append(store, "run-a")

    assert [e.sequence for e in store.list("run-a")] == [1, 2]
    assert [e.sequence for e in store.list("run-b")] == [1]


def test_reads_never_leak_across_runs(store: EventStore) -> None:
    for i in range(3):
        _append(store, "run-a", f"a{i}")
    for i in range(5):
        _append(store, "run-b", f"b{i}")

    assert len(store.list("run-a")) == 3
    assert len(store.list("run-b")) == 5
    assert store.count("run-a") == 3
    assert store.count("run-b") == 5


def test_sequence_of_resolves_cursor(store: EventStore) -> None:
    first = _append(store, "run-a", "a0")
    second = _append(store, "run-a", "a1")
    assert store.sequence_of(first.event_id) == 1
    assert store.sequence_of(second.event_id) == 2
    assert store.sequence_of("does-not-exist") is None


def test_get_by_event_id(store: EventStore) -> None:
    event = _append(store, "run-a")
    found = store.get_by_event_id(event.event_id)
    assert found is not None
    assert found.run_id == "run-a"
    assert store.get_by_event_id(new_event_id()) is None


# ── Append-only guarantees ────────────────────────────────────────────────────


def test_store_exposes_no_update_or_delete_api() -> None:
    """Append-only is enforced by the interface, not by convention."""
    public = {name for name in dir(EventStore) if not name.startswith("_")}
    assert not (
        public
        & {"update", "delete", "remove", "purge", "truncate", "clear", "replace"}
    )
    # The only private helper defined on the class is the sequence allocator.
    private_helpers = {
        name
        for name in vars(EventStore)
        if name.startswith("_") and not name.startswith("__")
    }
    assert private_helpers == {"_next_sequence"}


def test_existing_events_are_never_mutated_by_later_appends(store: EventStore) -> None:
    first = _append(store, "run-a", "original")
    _append(store, "run-a", "later")

    reread = store.get_by_event_id(first.event_id)
    assert reread is not None
    assert reread.message == "original"
    assert reread.sequence == 1


def test_events_survive_a_new_store_instance(store: EventStore) -> None:
    _append(store, "run-a", "persisted")
    fresh = EventStore()
    assert [e.message for e in fresh.list("run-a")] == ["persisted"]
