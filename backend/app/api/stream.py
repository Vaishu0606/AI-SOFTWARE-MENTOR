"""
Server-Sent Events streaming — Phase 6.

GET /api/mentor/runs/{run_id}/events
    Streams this run's workflow events as ``text/event-stream``.

Why SSE and not WebSockets
--------------------------
Workflow progress is one-directional (server → client) and resume-friendly.
SSE is plain HTTP, works through every proxy, and is natively re-connectable
via the ``Last-Event-ID`` header. No WebSocket dependency is introduced.

Why polling SQLite rather than an in-memory broker
-------------------------------------------------
The event store is append-only and authoritative, so the stream can always
reconstruct the correct sequence.  Tailing it directly means:

* no events are lost between requests or across reconnects,
* no event can leak from another run (every query filters on ``run_id``),
* no external broker (Redis / Kafka / RabbitMQ) is required,
* the stream works correctly even if the run advanced before the client
  connected.

Termination
-----------
The stream closes cleanly when any of the following happens:

1. the run reaches a terminal state and all its events have been sent,
2. ``max_events`` events have been emitted,
3. ``timeout`` seconds have elapsed since the stream opened,
4. the client disconnects.

The durable history remains queryable through
``GET /api/mentor/runs/{run_id}`` and the orchestrator's event store, so
closing the stream never loses information.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app.core.config import settings
from app.models.workflow import WorkflowEvent, is_valid_run_id
from app.services.orchestrator import (
    MentorOrchestrator,
    get_default_orchestrator,
)
from app.services.run_store import RunNotFoundError
from app.services.state_machine import WorkflowStateMachine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mentor", tags=["mentor"])

SSE_MEDIA_TYPE = "text/event-stream"

#: How many events are read from the store per poll iteration.
_EVENT_BATCH = 200

#: Upper bound on a single event payload (defensive, not expected to trigger).
_MAX_EVENT_CHARS = 64_000


def orchestrator_dependency() -> MentorOrchestrator:
    """FastAPI dependency returning the shared orchestrator instance."""
    return get_default_orchestrator()


# ── Endpoint ──────────────────────────────────────────────────────────────────


@router.get(
    "/runs/{run_id}/events",
    summary="Stream workflow events for a run (Server-Sent Events)",
    response_class=StreamingResponse,
    responses={
        200: {
            "content": {SSE_MEDIA_TYPE: {"schema": {"type": "string"}}},
            "description": "Server-Sent Events stream of workflow events.",
        },
        400: {"description": "Malformed run ID."},
        404: {"description": "Unknown run."},
    },
)
async def stream_run_events(
    request: Request,
    run_id: str,
    timeout: float = Query(
        default=settings.sse_timeout_seconds,
        gt=0,
        le=3600,
        description="Seconds to keep the stream open before closing it.",
    ),
    max_events: int | None = Query(
        default=None,
        ge=1,
        le=1000,
        description="Close the stream after this many events have been sent.",
    ),
    last_event_id: str | None = Query(
        default=None,
        description=(
            "Resume cursor. Equivalent to the Last-Event-ID header; only events "
            "after this event are sent."
        ),
    ),
    orchestrator: MentorOrchestrator = Depends(orchestrator_dependency),
) -> StreamingResponse:
    """Stream the workflow events belonging to ``run_id``.

    Only this run's events are ever emitted.  The response content type is
    ``text/event-stream``.
    """
    if not is_valid_run_id(run_id):
        raise HTTPException(
            status_code=400,
            detail=(
                "run_id must be a server-issued UUID4 identifier "
                "and contain only lowercase hexadecimal characters"
            ),
        )

    # Resolve the run *before* opening the stream so that an unknown run is a
    # clean 404 rather than a hanging 200 response.
    try:
        orchestrator.get_run(run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"Run not found: {run_id}") from exc

    # Last-Event-ID header is the standard SSE resume mechanism; the query
    # parameter exists for clients that cannot set headers (e.g. EventSource
    # polyfills and curl).
    resume_from = last_event_id or request.headers.get("last-event-id")

    return StreamingResponse(
        _event_stream(
            request=request,
            orchestrator=orchestrator,
            run_id=run_id,
            timeout=timeout,
            max_events=max_events,
            resume_from=resume_from,
        ),
        media_type=SSE_MEDIA_TYPE,
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            # Disable proxy buffering so events are delivered immediately.
            "X-Accel-Buffering": "no",
        },
    )


# ── Stream generator ──────────────────────────────────────────────────────────


async def _event_stream(
    request: Request,
    orchestrator: MentorOrchestrator,
    run_id: str,
    timeout: float,
    max_events: int | None,
    resume_from: str | None,
) -> AsyncIterator[str]:
    """Yield SSE frames for ``run_id`` until a termination condition is met."""
    started_at = time.monotonic()
    deadline = started_at + timeout
    poll_interval = max(0.01, float(settings.sse_poll_interval_seconds))
    heartbeat_interval = max(1.0, float(settings.sse_heartbeat_seconds))

    cursor = _resolve_cursor(orchestrator, run_id, resume_from)
    sent = 0
    last_activity = time.monotonic()
    reason = "completed"

    logger.info("SSE stream opened for run %s (cursor=%s)", run_id, cursor)

    try:
        # Flush headers immediately so the client's connection opens now
        # rather than after the first event.
        yield ": mentor-workflow-stream-open\nretry: 3000\n\n"

        while True:
            if await _is_disconnected(request):
                reason = "client_disconnected"
                break

            events = orchestrator.tail_events(
                run_id, after_sequence=cursor, limit=_EVENT_BATCH
            )

            for event in events:
                cursor = event.sequence
                frame = _format_event(event)
                if frame is None:  # pragma: no cover - defensive
                    logger.error(
                        "Skipping unserialisable event %s for run %s",
                        event.event_id,
                        run_id,
                    )
                    continue
                yield frame
                sent += 1
                last_activity = time.monotonic()
                if max_events is not None and sent >= max_events:
                    reason = "max_events"
                    break

            if max_events is not None and sent >= max_events:
                break

            now = time.monotonic()
            if now >= deadline:
                reason = "timeout"
                break

            if await _is_disconnected(request):
                reason = "client_disconnected"
                break

            # Only close on a terminal run once its events have been drained.
            if not events:
                run = orchestrator.get_run(run_id)
                if WorkflowStateMachine.is_terminal(run.current_state):
                    reason = "run_terminal"
                    break

            if now - last_activity >= heartbeat_interval:
                # Comment frame: keeps proxies from closing an idle stream.
                yield ": keep-alive\n\n"
                last_activity = now

            await asyncio.sleep(poll_interval)

    except asyncio.CancelledError:  # client hung up mid-stream
        reason = "cancelled"
        raise
    except GeneratorExit:  # pragma: no cover - generator closed by the server
        reason = "generator_closed"
        raise
    except Exception:  # noqa: BLE001 - never leak a traceback to the client
        logger.exception("SSE stream failed for run %s", run_id)
        yield _format_comment("stream-error")
        reason = "error"
    finally:
        logger.info(
            "SSE stream for run %s closed (%s) after %d event(s)",
            run_id,
            reason,
            sent,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


def _format_event(event: WorkflowEvent) -> str | None:
    """Render one event as an SSE frame, or None when it cannot be encoded."""
    try:
        data = json.dumps(event.model_dump(mode="json"), sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return None
    if len(data) > _MAX_EVENT_CHARS:  # pragma: no cover - defensive
        data = json.dumps(
            {
                "event_id": event.event_id,
                "run_id": event.run_id,
                "sequence": event.sequence,
                "event_type": event.event_type.value,
                "state": event.state.value,
                "status": event.status.value,
                "message": event.message[:2000],
                "payload_truncated": True,
            },
            sort_keys=True,
        )
    return (
        f"id: {event.event_id}\n"
        f"event: {event.event_type.value}\n"
        f"data: {data}\n\n"
    )


def _format_comment(text: str) -> str:
    return f": {text}\n\n"


def _resolve_cursor(
    orchestrator: MentorOrchestrator, run_id: str, resume_from: str | None
) -> int:
    """Translate a resume cursor into a sequence number.

    Unknown or malformed cursors are ignored (the stream replays from the
    beginning) rather than failing the request.
    """
    if not resume_from:
        return 0
    sequence = orchestrator.event_sequence_for(run_id, resume_from)
    if sequence is None:
        logger.info(
            "Ignoring unknown SSE resume cursor %s for run %s", resume_from, run_id
        )
        return 0
    return sequence


async def _is_disconnected(request: Request) -> bool:
    """Return True when the client has gone away.  Never raises."""
    try:
        return await request.is_disconnected()
    except Exception:  # noqa: BLE001 - transport quirks must not kill the stream
        return False
