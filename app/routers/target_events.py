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

from typing import Annotated

import uuid6
from fastapi import APIRouter, Depends, HTTPException, status
from lap_schemas.v1 import TargetEvent, TargetEventCreate
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
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
    return _to_response(row)
