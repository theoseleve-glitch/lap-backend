"""T001/T002 (sprint2-backend) — GET /api/v1/driver/timeline.

Returns the authenticated driver's session records, newest first,
with achievement badges (currently: `personal_best`).

Cursor (T002): time-anchored, base64-encoded, sha256-tagged token
bound to the caller's user_id. See `app/cursor.py`. Pagination
is keyset-style (`WHERE created_at < last_seen_at`) rather than
offset-based — stable across concurrent inserts and resistant
to forgery from another caller.

Cross-driver isolation is enforced by joining `sessions` to
`driver_profiles` on the caller's `User.id`. The DB-level RLS
policy (T074, currently BLOCKED) would be belt-and-braces; for
now this app-layer scope is the gate.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from lap_schemas.v1 import TimelineEntry, TimelineResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cursor import decode_cursor, encode_cursor
from app.deps import get_current_user, get_db
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import Session as SessionRow
from infra.orm import SessionRecord as SessionRecordRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/driver", tags=["timeline"])


_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


@router.get("/timeline", response_model=TimelineResponse)
async def get_driver_timeline(
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
    # T003 — composable filters. All AND together, missing = no-op.
    # Bare `= None` (no Query wrapper) is enough — FastAPI infers
    # query-param semantics from the type annotation, and ruff's
    # B008 doesn't trip on simple defaults.
    source: str | None = None,
    track_id: UUID | None = None,
    car_id: UUID | None = None,
    from_date: datetime | None = None,
    to_date: datetime | None = None,
) -> TimelineResponse:
    # T002 — decode caller-bound cursor. None / invalid / forged →
    # treated as "no cursor" (start from the top), failing closed
    # by silently restarting rather than 401-ing on stale tokens.
    last_seen_at = (
        decode_cursor(user.id, cursor) if cursor is not None else None
    )

    # Resolve the caller's driver_id once. A user with no
    # DriverProfile row has no sessions by definition.
    driver_id = (
        await db.execute(
            select(DriverProfileRow.id).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if driver_id is None:
        return TimelineResponse(entries=[], next_cursor=None)

    # Fetch one extra row to detect "is there a next page?" without
    # a separate COUNT query.
    page_query = (
        select(SessionRow.id, SessionRow.created_at, SessionRecordRow.payload)
        .join(SessionRecordRow, SessionRecordRow.session_id == SessionRow.id)
        .where(SessionRow.driver_id == driver_id)
    )
    if last_seen_at is not None:
        page_query = page_query.where(SessionRow.created_at < last_seen_at)
    # T003 — manifest filters via JSON extraction (SQLite + PG both
    # support `json_extract`; on PG it maps to jsonb_extract_path).
    # All filters are equality on a single string field.
    if source is not None:
        page_query = page_query.where(
            func.json_extract(SessionRow.manifest, "$.source") == source
        )
    if track_id is not None:
        page_query = page_query.where(
            func.json_extract(SessionRow.manifest, "$.track_id") == str(track_id)
        )
    if car_id is not None:
        page_query = page_query.where(
            func.json_extract(SessionRow.manifest, "$.car_id") == str(car_id)
        )
    if from_date is not None:
        page_query = page_query.where(SessionRow.created_at >= from_date)
    if to_date is not None:
        page_query = page_query.where(SessionRow.created_at < to_date)
    page_query = page_query.order_by(SessionRow.created_at.desc()).limit(
        limit + 1
    )
    rows = (await db.execute(page_query)).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    # Personal-best badge: compare each row's best_lap_ms against
    # the driver's all-time minimum. One small extra query — cheap
    # vs. denormalizing PB onto the row.
    best_lap_ms = (
        await db.execute(
            select(SessionRecordRow.payload)
            .join(SessionRow, SessionRow.id == SessionRecordRow.session_id)
            .where(SessionRow.driver_id == driver_id)
        )
    ).all()
    pb_value: int | None = None
    for (payload,) in best_lap_ms:
        candidate = payload.get("best_lap_ms") if payload else None
        if candidate is None:
            continue
        if pb_value is None or candidate < pb_value:
            pb_value = candidate

    entries: list[TimelineEntry] = []
    last_created_at = None
    for session_id, created_at, payload in page_rows:
        payload_dict: dict[str, Any] = payload or {}
        row_best = payload_dict.get("best_lap_ms")
        badges: list[str] = []
        if pb_value is not None and row_best == pb_value:
            badges.append("personal_best")
        entries.append(
            TimelineEntry(
                session_id=_as_uuid(session_id),
                processed_at=_iso_to_dt(payload_dict.get("processed_at")),
                best_lap_ms=row_best,
                total_laps=payload_dict.get("total_laps", 0) or 0,
                valid_laps=payload_dict.get("valid_laps", 0) or 0,
                badges=badges,
            )
        )
        last_created_at = created_at

    # T002 — mint next_cursor anchored to the last row's created_at
    # so resume issues `WHERE created_at < last`, not `OFFSET N`.
    next_cursor = (
        encode_cursor(user.id, last_created_at)
        if has_more and last_created_at is not None
        else None
    )
    return TimelineResponse(entries=entries, next_cursor=next_cursor)


def _as_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    return UUID(str(value))


def _iso_to_dt(value: Any) -> Any:
    """SessionRecord.payload stores processed_at as an ISO string
    when round-tripped through JSON. Pydantic accepts either str
    or datetime, so we just pass it through and let it parse."""
    return value
