"""T001 (sprint2-backend) — GET /api/v1/driver/timeline.

Returns the authenticated driver's session records, newest first,
with achievement badges (currently: `personal_best`).

Cursor encoding is offset-based for T001 — opaque on the wire so
T002 can swap it for a hashed token without breaking clients.
The contract is "string in, string out, null = no more pages".

Cross-driver isolation is enforced by joining `sessions` to
`driver_profiles` on the caller's `User.id`. The DB-level RLS
policy (T074, currently BLOCKED) would be belt-and-braces; for
now this app-layer scope is the gate.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from lap_schemas.v1 import TimelineEntry, TimelineResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import Session as SessionRow
from infra.orm import SessionRecord as SessionRecordRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/driver", tags=["timeline"])


_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100


def _decode_cursor(cursor: str | None) -> int:
    """Cursor format for T001 is `<offset>` as a decimal string.
    Opaque to clients — they only echo what `next_cursor` returned.
    T002 will swap to a hashed format with the same wire shape."""
    if cursor is None or cursor == "":
        return 0
    try:
        offset = int(cursor)
    except ValueError:
        return 0
    return max(offset, 0)


def _encode_cursor(offset: int) -> str:
    return str(offset)


@router.get("/timeline", response_model=TimelineResponse)
async def get_driver_timeline(
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> TimelineResponse:
    offset = _decode_cursor(cursor)

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
        .order_by(SessionRow.created_at.desc())
        .offset(offset)
        .limit(limit + 1)
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
    for session_id, _created_at, payload in page_rows:
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

    next_cursor = _encode_cursor(offset + limit) if has_more else None
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
