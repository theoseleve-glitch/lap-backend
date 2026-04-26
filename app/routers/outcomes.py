"""T222 — Driver-facing outcome list endpoint.

`GET /api/v1/driver/:id/outcomes?window=Nd` returns the
driver's recent ActionOutcome rows, filtered by a "last N
days" window on `measured_at` (default 30d). Carries a
server-computed `mean_efficacy` summary so dashboards don't
re-aggregate items client-side and risk drift between the
headline number and the row data.

Auth posture mirrors T217: 404 for cross-driver IDs so they're
not enumerable. Window param is "Nd" only (positive int +
literal 'd'); malformed strings return 422 to keep the contract
tight rather than silently coercing.
"""

import re
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from lap_schemas.v1 import ActionOutcomeList, ActionOutcomeListItem
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import ActionOutcome as ActionOutcomeRow
from infra.orm import ActionPrescription as ActionPrescriptionRow
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/driver", tags=["outcomes"])

_WINDOW_RE = re.compile(r"^(\d+)d$")
_DEFAULT_WINDOW = "30d"


def _parse_window(window: str) -> int:
    """Returns N for 'Nd' where N is a positive int. 422-style
    HTTPException for anything else."""
    match = _WINDOW_RE.fullmatch(window)
    if match is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="window_must_be_Nd",
        )
    days = int(match.group(1))
    if days <= 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="window_must_be_positive",
        )
    return days


@router.get(
    "/{driver_id}/outcomes",
    response_model=ActionOutcomeList,
)
async def list_driver_outcomes(
    driver_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    window: Annotated[str, Query()] = _DEFAULT_WINDOW,
) -> ActionOutcomeList:
    days = _parse_window(window)
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    # Cross-driver enumeration must collapse to 404 — same
    # posture as T217 so driver_ids aren't probeable.
    if profile is None or profile.id != driver_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="driver_not_found",
        )
    cutoff = datetime.now(UTC) - timedelta(days=days)
    rows: Any = (
        await db.execute(
            select(ActionOutcomeRow)
            .join(
                ActionPrescriptionRow,
                ActionPrescriptionRow.id
                == ActionOutcomeRow.prescription_id,
            )
            .where(ActionPrescriptionRow.driver_id == driver_id)
            .where(ActionOutcomeRow.measured_at >= cutoff)
        )
    ).scalars().all()
    items = tuple(
        ActionOutcomeListItem(
            id=row.id,
            prescription_id=row.prescription_id,
            measured_at=row.measured_at,
            success_condition_met=row.success_condition_met,
            measured_value=row.measured_value,
            delta_vs_baseline=row.delta_vs_baseline,
            confidence_change=row.confidence_change,
            prescription_efficacy_score=row.prescription_efficacy_score,
        )
        for row in rows
    )
    mean_efficacy = (
        sum(r.prescription_efficacy_score for r in rows) / len(rows)
        if rows
        else None
    )
    return ActionOutcomeList(
        items=items,
        total=len(items),
        mean_efficacy=mean_efficacy,
        window_days=days,
    )
