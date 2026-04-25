"""SSE endpoint for ingest job progress (T112, T113).

`GET /api/v1/jobs/{job_id}/events` opens a Server-Sent Events stream
the client subscribes to with EventSource (or a polyfill that
supports the Authorization header).

T112 established the wire shape: an initial `hello` event + periodic
keepalive comments. T113 forwards real lifecycle events from the
`JobEventBus`: queued, processing, stage, completed, failed.
Terminal events (completed, failed) close the stream — once a job
is done there's nothing left to push.

Tunables (env):
  SSE_KEEPALIVE_SECONDS  — keepalive interval (default 15s)
  SSE_MAX_KEEPALIVES     — finite cap for tests (production = unset = forever)
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse

from app.deps import get_current_user, get_job_event_bus
from infra.job_event_bus import JobEventBusProtocol, subscription
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

TERMINAL_EVENTS = frozenset({"completed", "failed"})


def _keepalive_seconds() -> float:
    return float(os.environ.get("SSE_KEEPALIVE_SECONDS", "15"))


def _max_keepalives() -> int | None:
    raw = os.environ.get("SSE_MAX_KEEPALIVES")
    return int(raw) if raw else None


def _format_event(name: str, data: dict[str, object]) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


async def _event_stream(
    job_id: str,
    request: Request,
    bus: JobEventBusProtocol,
) -> AsyncIterator[bytes]:
    """Hello → bus events → terminal-event close (or keepalives until disconnect)."""
    yield _format_event("hello", {"job_id": job_id})

    interval = _keepalive_seconds()
    cap = _max_keepalives()
    sent_keepalives = 0

    with subscription(bus, job_id) as queue:
        while True:
            if await request.is_disconnected():
                return
            try:
                name, data = await asyncio.wait_for(
                    queue.get(), timeout=interval
                )
            except TimeoutError:
                yield b": keep-alive\n\n"
                sent_keepalives += 1
                if cap is not None and sent_keepalives >= cap:
                    return
                continue

            yield _format_event(name, data)
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
) -> StreamingResponse:
    _ = user
    return StreamingResponse(
        _event_stream(job_id, request, bus),
        media_type="text/event-stream",
        headers={
            # Disable proxy buffering — SSE needs flush-after-each-event.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
