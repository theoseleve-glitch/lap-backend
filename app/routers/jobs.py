"""SSE endpoint for ingest job progress (T112).

`GET /api/v1/jobs/{job_id}/events` opens a Server-Sent Events stream
the client subscribes to with EventSource (or a polyfill that
supports the Authorization header).

T112 establishes the wire shape and proves the stream stays alive:
an initial `hello` event announces the connection, then a
`: keep-alive` SSE comment fires every `KEEPALIVE_SECONDS` so
proxies don't time the idle connection out. T113 layers
queued/processing/stage/completed/failed events on top.

Tunables (env):
  SSE_KEEPALIVE_SECONDS  — interval between keepalive comments
  SSE_MAX_KEEPALIVES     — finite cap for tests (production = unset = forever)
"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse

from app.deps import get_current_user
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])


def _keepalive_seconds() -> float:
    return float(os.environ.get("SSE_KEEPALIVE_SECONDS", "15"))


def _max_keepalives() -> int | None:
    raw = os.environ.get("SSE_MAX_KEEPALIVES")
    return int(raw) if raw else None


async def _event_stream(
    job_id: str, request: Request
) -> AsyncIterator[bytes]:
    """Yield the initial hello event then keepalive comments until the
    client disconnects (or the optional cap fires in tests)."""
    hello = (
        f"event: hello\n"
        f"data: {json.dumps({'job_id': job_id})}\n\n"
    )
    yield hello.encode()

    interval = _keepalive_seconds()
    cap = _max_keepalives()
    sent = 0
    while True:
        await asyncio.sleep(interval)
        if await request.is_disconnected():
            return
        yield b": keep-alive\n\n"
        sent += 1
        if cap is not None and sent >= cap:
            return


@router.get(
    "/{job_id}/events",
    status_code=status.HTTP_200_OK,
    responses={200: {"content": {"text/event-stream": {}}}},
)
async def job_events(
    job_id: str,
    user: Annotated[UserRow, Depends(get_current_user)],
    request: Request,
) -> StreamingResponse:
    _ = user
    return StreamingResponse(
        _event_stream(job_id, request),
        media_type="text/event-stream",
        headers={
            # Disable proxy buffering — SSE needs flush-after-each-event.
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
