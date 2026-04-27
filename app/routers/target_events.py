"""T082 (sprint2-backend) — POST /api/v1/driver/target-events.

Pins an upcoming sim event or real-life session for the
authenticated driver. The request body (TargetEventCreate)
omits id (server-generated UUID7) and driver_id (auth-derived
to close the IDOR vector). Response is the full TargetEvent
shape from ADR §5.7.

Subsequent endpoints (T083 DELETE, T084 GET list) will hang off
this same `driver_router`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

import uuid6
from fastapi import APIRouter, Depends, HTTPException, Response, status
from lap_schemas.v1 import (
    TargetEvent,
    TargetEventCountdown,
    TargetEventCreate,
    TargetEventList,
    TrackDayCreate,
)
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from domain.readiness_recompute import recompute_driver_readiness
from infra.orm import DriverProfile
from infra.orm import TargetEvent as TargetEventRow
from infra.orm import User as UserRow

driver_router = APIRouter(prefix="/api/v1/driver", tags=["target_events"])


def _to_response(row: TargetEventRow) -> TargetEvent:
    # `kind` is a free-text String(32) at the DB layer (T081 chose
    # Pydantic-side enforcement over a CHECK constraint to deliver
    # useful 422s). Pydantic validates the Literal here on
    # construction — a stray DB-level kind raises and surfaces as
    # 500, exactly the loud failure we want.
    return TargetEvent(
        id=row.id,
        name=row.name,
        kind=row.kind,  # type: ignore[arg-type]
        starts_at=row.starts_at,
        ends_at=row.ends_at,
        track_id=row.track_id,
        car_id=row.car_id,
        competitive_weight=row.competitive_weight,
        notes=row.notes,
    )


@driver_router.post(
    "/target-events",
    response_model=TargetEvent,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "driver_not_found"},
    },
)
async def create_target_event(
    payload: TargetEventCreate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TargetEvent:
    profile = (
        await db.execute(
            select(DriverProfile).where(DriverProfile.user_id == user.id)
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="driver_not_found",
        )

    row = TargetEventRow(
        id=uuid6.uuid7(),
        driver_id=profile.id,
        name=payload.name,
        kind=payload.kind,
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        track_id=payload.track_id,
        car_id=payload.car_id,
        competitive_weight=payload.competitive_weight,
        notes=payload.notes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    # T089 — readiness must reflect the new pin so T090's plan
    # generation reads target-aware inputs on the next request.
    await recompute_driver_readiness(db, driver_id=profile.id)
    return _to_response(row)


@driver_router.delete(
    "/target-events/{event_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        404: {
            "description": (
                "driver_not_found OR target_event_not_found "
                "(same status for either to avoid enumeration)"
            )
        },
    },
)
async def delete_target_event(
    event_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    profile = (
        await db.execute(
            select(DriverProfile).where(DriverProfile.user_id == user.id)
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="driver_not_found",
        )
    # Authz pin: filter by BOTH id and driver_id so an authenticated
    # user can never delete another driver's pin by guessing UUIDs.
    # rowcount==0 means either "no such pin" or "exists but belongs
    # to someone else"; surface the same 404 either way to avoid
    # leaking which UUIDs are valid pins.
    result = await db.execute(
        delete(TargetEventRow).where(
            TargetEventRow.id == event_id,
            TargetEventRow.driver_id == profile.id,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="target_event_not_found",
        )
    await db.commit()
    # T089 — readiness must reflect the unpin (target_event=None
    # if no remaining pins, or the next-soonest one).
    await recompute_driver_readiness(db, driver_id=profile.id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@driver_router.get(
    "/target-events",
    response_model=TargetEventList,
    responses={404: {"description": "driver_not_found"}},
)
async def list_target_events(
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TargetEventList:
    profile = (
        await db.execute(
            select(DriverProfile).where(DriverProfile.user_id == user.id)
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="driver_not_found",
        )
    # Sort by starts_at ASC at the DB layer — soonest-first matches
    # the UI contract (clients display list[0] as "your next event").
    rows = (
        await db.execute(
            select(TargetEventRow)
            .where(TargetEventRow.driver_id == profile.id)
            .order_by(TargetEventRow.starts_at.asc())
        )
    ).scalars().all()
    return TargetEventList(
        target_events=tuple(_to_response(row) for row in rows),
    )


@driver_router.get(
    "/target-events/countdown",
    response_model=TargetEventCountdown,
    responses={404: {"description": "driver_not_found"}},
)
async def get_target_event_countdown(
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TargetEventCountdown:
    """T093 — server-side countdown for the UI. Returns the
    soonest UPCOMING TargetEvent for the authed driver plus
    seconds_until_start / days_until_start helpers and a
    server_now stamp the client uses to detect local-clock drift.
    target_event=None when the driver has no upcoming pin —
    distinct from 404 (driver_not_found)."""
    profile = (
        await db.execute(
            select(DriverProfile).where(DriverProfile.user_id == user.id)
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="driver_not_found",
        )
    server_now = datetime.now(UTC)
    # Soonest upcoming: same selector recompute_driver_readiness
    # uses (T089) — keeps the countdown consistent with what the
    # readiness pipeline considers "the next event".
    row = (
        await db.execute(
            select(TargetEventRow)
            .where(
                TargetEventRow.driver_id == profile.id,
                TargetEventRow.starts_at >= server_now,
            )
            .order_by(TargetEventRow.starts_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return TargetEventCountdown(
            target_event=None,
            seconds_until_start=None,
            days_until_start=None,
            server_now=server_now,
        )
    # SQLite's DateTime(timezone=True) strips tz on cross-object
    # reload (Postgres preserves). Re-tag as UTC if naive so the
    # subtraction below stays portable across both dialects.
    starts_at = row.starts_at
    if starts_at.tzinfo is None:
        starts_at = starts_at.replace(tzinfo=UTC)
    seconds = int((starts_at - server_now).total_seconds())
    return TargetEventCountdown(
        target_event=_to_response(row),
        seconds_until_start=seconds,
        days_until_start=seconds // 86400,
        server_now=server_now,
    )


@driver_router.post(
    "/track-days",
    response_model=TargetEvent,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "driver_not_found"}},
)
async def create_track_day(
    payload: TrackDayCreate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TargetEvent:
    """T088 — manual real-life event add. UX-friendly wrapper
    over the TargetEvent table: takes a free-text venue_name
    (no catalog FK required), persists with kind=real_track_day.
    venue_name is folded into the event's `name` so the GET
    list surfaces it without TargetEvent needing a dedicated
    venue column.
    """
    profile = (
        await db.execute(
            select(DriverProfile).where(DriverProfile.user_id == user.id)
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="driver_not_found",
        )

    row = TargetEventRow(
        id=uuid6.uuid7(),
        driver_id=profile.id,
        # Fold venue into the event name so the existing
        # TargetEvent shape carries the real-life context
        # without a schema migration. "Track day @ Venue"
        # makes the source visible to GET-list consumers.
        name=f"Track day @ {payload.venue_name}",
        kind="real_track_day",
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        track_id=None,
        car_id=None,
        competitive_weight=payload.competitive_weight,
        notes=payload.notes,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    # T089 — readiness recompute (track-day uses same hook).
    await recompute_driver_readiness(db, driver_id=profile.id)
    return _to_response(row)
