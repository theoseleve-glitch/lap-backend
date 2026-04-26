"""T011 (sprint2-backend) — POST /api/v1/insights/{id}/flag.

Canonical top-level URL for the user-flagged false-positive
endpoint. Same mutation as the legacy
`/api/v1/coaching/insights/{id}/flag` (T205) but addressable as a
top-level resource — clients should prefer this URL.

The legacy coaching-prefixed route stays in place; deprecation
is out of scope for T011 (minimal diffs).

"Decrements rule confidence" (sprint task wording) is implicit:
setting `flagged_false_positive` raises the rule's flagged-rate
when surfaced through `compute_high_false_positive_rules`. There
is no per-rule confidence column to mutate — the FP rate IS the
confidence signal.

Cross-driver isolation: 404 (not 403) for absent or not-owned
insights — same posture as `sessions.get_session` so insight ids
aren't enumerable.

Idempotent: re-flagging is a 200 no-op so a double-tap UI doesn't
double-count in the aggregation.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import CoachingInsight as CoachingInsightRow
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import User as UserRow

router = APIRouter(prefix="/api/v1/insights", tags=["insights"])


@router.post("/{insight_id}/flag")
async def flag_insight_false_positive(
    insight_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict[str, Any]:
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    insight = (
        await db.execute(
            select(CoachingInsightRow).where(
                CoachingInsightRow.id == insight_id
            )
        )
    ).scalar_one_or_none()
    if (
        insight is None
        or profile is None
        or insight.driver_id != profile.id
    ):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="insight_not_found",
        )
    if not insight.flagged_false_positive:
        insight.flagged_false_positive = True
        await db.commit()
    return {
        "id": str(insight.id),
        "flagged_false_positive": insight.flagged_false_positive,
    }
