"""T089 (sprint2-backend) — driver readiness recompute.

Triggered after a TargetEvent pin/unpin so DriverReadinessState
reflects the new target context. Picks the SOONEST UPCOMING pin
(starts_at >= now) as the target_event JSON; past events don't
shape near-term readiness.

Inputs other than event_pressure_input are placeholder defaults
(0.5 each) at this point in the sprint. T090's plan generation
will consume the readiness state; the per-input formula behind
volume/quality/recency etc. lands in a follow-up domain task.
event_pressure_input mirrors the pinned event's
competitive_weight directly per ADR §5.7 — that's the wiring T089
needs to establish so T090 has a non-trivial signal to read.

History-style: each call appends a new DriverReadinessState row.
There is no UNIQUE on driver_id so the snapshot trail is
preserved (T093 countdown UI and admin trends view use it).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import uuid6
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import DriverReadinessState, TargetEvent

# Placeholder defaults for the seven non-event inputs. They get
# their real values when the per-signal compute domains land.
# 0.5 = neutral so the resulting readiness_score isn't biased.
_DEFAULT_INPUT = 0.5


def _serialize_target_event(row: TargetEvent) -> dict[str, Any]:
    """Convert a TargetEvent ORM row to the JSON shape stored on
    DriverReadinessState.target_event. Mirrors lap-schemas
    TargetEvent fields verbatim (str-cast UUIDs and datetimes for
    JSON portability)."""
    return {
        "id": str(row.id),
        "name": row.name,
        "kind": row.kind,
        "starts_at": row.starts_at.isoformat(),
        "ends_at": row.ends_at.isoformat() if row.ends_at else None,
        "track_id": str(row.track_id) if row.track_id else None,
        "car_id": str(row.car_id) if row.car_id else None,
        "competitive_weight": row.competitive_weight,
        "notes": row.notes,
    }


async def recompute_driver_readiness(
    db: AsyncSession,
    *,
    driver_id: UUID,
    now: datetime | None = None,
) -> DriverReadinessState:
    """Append a fresh DriverReadinessState snapshot for driver_id.

    Picks the soonest TargetEvent with starts_at >= now as the
    target. Returns the inserted row. Caller's responsibility is
    to call this AFTER a TargetEvent pin/unpin commit so the
    query sees the updated row set.
    """
    computed_at = now or datetime.now(UTC)
    target_row = (
        await db.execute(
            select(TargetEvent)
            .where(
                TargetEvent.driver_id == driver_id,
                TargetEvent.starts_at >= computed_at,
            )
            .order_by(TargetEvent.starts_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    target_json = (
        _serialize_target_event(target_row)
        if target_row is not None
        else None
    )
    event_pressure = (
        target_row.competitive_weight if target_row is not None else 0.0
    )

    state = DriverReadinessState(
        id=uuid6.uuid7(),
        driver_id=driver_id,
        target_event=target_json,
        computed_at=computed_at,
        # Placeholder readiness — neutral midpoint while the per-
        # input formulas are still under construction. T090 reads
        # the inputs directly so the score being a placeholder
        # doesn't block downstream wiring.
        readiness_score=50.0,
        volume_input=_DEFAULT_INPUT,
        quality_input=_DEFAULT_INPUT,
        recency_input=_DEFAULT_INPUT,
        consistency_input=_DEFAULT_INPUT,
        unresolved_weaknesses_input=_DEFAULT_INPUT,
        familiarity_input=_DEFAULT_INPUT,
        event_pressure_input=event_pressure,
        recovery_input=_DEFAULT_INPUT,
        limiting_factor="placeholder",
        likely_gain_area="placeholder",
        recommended_sessions_before_event=0,
    )
    db.add(state)
    await db.commit()
    await db.refresh(state)
    return state
