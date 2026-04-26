"""T075 (sprint2-backend) — GET /api/v1/endurance/events/{event_id}.

Roll-up endpoint for an endurance race weekend. Combines:
  - planned shape from EnduranceEvent (T067) — name, total
    duration, planned stint count, denormalized driver roster.
  - recorded aggregates from sessions linked via
    SessionManifest.endurance_event_id (T066) and their
    StintRecords (T069) — session count, stint count, means.

mean_* aggregates are None when no stints recorded yet (early
state of an event). Coercing to 0.0 would conflate "nothing
logged" with "perfect preservation" — distinct UI states.

404 if event_id doesn't exist in the DB. 401 unauthenticated.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from lap_schemas.v1 import EnduranceEventSummary
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import EnduranceEvent, StintRecord
from infra.orm import Session as SessionRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/endurance", tags=["endurance"])


@router.get(
    "/events/{event_id}",
    response_model=EnduranceEventSummary,
    responses={404: {"description": "endurance_event_not_found"}},
)
async def get_endurance_event_summary(
    event_id: UUID,
    _user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> EnduranceEventSummary:
    event = (
        await db.execute(
            select(EnduranceEvent).where(EnduranceEvent.id == event_id)
        )
    ).scalar_one_or_none()
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="endurance_event_not_found",
        )

    # Sessions linked via manifest.endurance_event_id (T066). Use
    # Python-side filter for SQLite/Postgres portability — the JSON
    # extraction syntax differs between dialects.
    target = str(event_id)
    rows = (
        await db.execute(select(SessionRow.id, SessionRow.manifest))
    ).all()
    linked_ids = [
        sid
        for sid, manifest in rows
        if isinstance(manifest, dict)
        and manifest.get("endurance_event_id") == target
    ]
    recorded_session_count = len(linked_ids)

    if not linked_ids:
        return EnduranceEventSummary(
            id=event.id,
            event_name=event.event_name,
            total_duration_seconds=event.total_duration_seconds,
            stint_count=event.stint_count,
            driver_ids=[UUID(d) for d in event.driver_ids],
            recorded_session_count=0,
            recorded_stint_count=0,
            mean_lap_time_decay_seconds=None,
            mean_fuel_used_liters=None,
        )

    agg_row = (
        await db.execute(
            select(
                func.count(StintRecord.id),
                func.avg(StintRecord.lap_time_decay_seconds),
                func.avg(StintRecord.fuel_used_liters),
            ).where(StintRecord.session_id.in_(linked_ids))
        )
    ).one()
    stint_count, mean_decay, mean_fuel = agg_row
    return EnduranceEventSummary(
        id=event.id,
        event_name=event.event_name,
        total_duration_seconds=event.total_duration_seconds,
        stint_count=event.stint_count,
        driver_ids=[UUID(d) for d in event.driver_ids],
        recorded_session_count=recorded_session_count,
        recorded_stint_count=int(stint_count or 0),
        mean_lap_time_decay_seconds=(
            float(mean_decay) if mean_decay is not None else None
        ),
        mean_fuel_used_liters=(
            float(mean_fuel) if mean_fuel is not None else None
        ),
    )
