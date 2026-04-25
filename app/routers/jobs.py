"""SSE endpoint for ingest job progress (T112, T113, T114).

`GET /api/v1/jobs/{job_id}/events` opens a Server-Sent Events stream
the client subscribes to with EventSource (or a polyfill that
supports the Authorization header).

T112 established the wire shape: an initial `hello` event + periodic
keepalive comments. T113 forwards real lifecycle events from the
`JobEventBus`: queued, processing, stage, completed, failed.
Terminal events (completed, failed) close the stream — once a job
is done there's nothing left to push. T114 adds Last-Event-ID
reconnection support: each event carries a per-job monotonic id, and
a reconnecting client passes its last-seen id via the standard SSE
header to resume from the next event.

Tunables (env):
  SSE_KEEPALIVE_SECONDS  — keepalive interval (default 15s)
  SSE_MAX_KEEPALIVES     — finite cap for tests (production = unset = forever)
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import StreamingResponse

from app.deps import get_current_user, get_job_event_bus
from infra.job_event_bus import JobEventBusProtocol, subscription
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

TERMINAL_EVENTS = frozenset({"completed", "failed"})
HELLO_EVENT_ID = 0


def _keepalive_seconds() -> float:
    return float(os.environ.get("SSE_KEEPALIVE_SECONDS", "15"))


def _max_keepalives() -> int | None:
    raw = os.environ.get("SSE_MAX_KEEPALIVES")
    return int(raw) if raw else None


def _format_event(event_id: int, name: str, data: dict[str, object]) -> bytes:
    return (
        f"id: {event_id}\nevent: {name}\n"
        f"data: {json.dumps(data)}\n\n"
    ).encode()


def _parse_last_event_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        # EventSource sends whatever it last received — corrupted input
        # should still recover with a full replay rather than 400ing.
        return None


async def _event_stream(
    job_id: str,
    request: Request,
    bus: JobEventBusProtocol,
    last_event_id: int | None,
) -> AsyncIterator[bytes]:
    """Hello (fresh connect only) → bus events → terminal close."""
    if last_event_id is None:
        yield _format_event(HELLO_EVENT_ID, "hello", {"job_id": job_id})

    interval = _keepalive_seconds()
    cap = _max_keepalives()
    sent_keepalives = 0

    with subscription(bus, job_id, after_id=last_event_id) as queue:
        while True:
            if await request.is_disconnected():
                return
            try:
                event_id, name, data = await asyncio.wait_for(
                    queue.get(), timeout=interval
                )
            except TimeoutError:
                yield b": keep-alive\n\n"
                sent_keepalives += 1
                if cap is not None and sent_keepalives >= cap:
                    return
                continue

            yield _format_event(event_id, name, data)
            if name in TERMINAL_EVENTS:
                return


@router.get(
    "/{job_id}/events",
    status_code=status.HTTP_200_OK,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def job_events(
    job_id: str,
    user: Annotated[UserRow, Depends(get_current_user)],
    bus: Annotated[JobEventBusProtocol, Depends(get_job_event_bus)],
    request: Request,
    last_event_id: Annotated[
        str | None, Header(alias="last-event-id")
    ] = None,
) -> StreamingResponse:
    _ = user
    return StreamingResponse(
        _event_stream(
            job_id, request, bus, _parse_last_event_id(last_event_id)
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
