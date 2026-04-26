"""T009 (sprint2-backend) — GET /api/v1/driver/skills/{dimension}.

Drilldown view of one of the 10 SkillDimension fields stored on
DriverProgressSnapshot. Returns the dimension's current value,
chronological trend, best/worst session refs, and a list of open
WeaknessEpisode rows whose category maps to the dimension's
domain.

Cross-driver isolation: 404 (not 403) for users without a
DriverProfile — mirrors `weaknesses.get_weakness_detail`.

Unknown dimension → 422 from FastAPI's path-param Literal
validation (no manual checks needed; the type is the contract).

Why best/worst is None when only one snapshot exists: a single
data point has no notion of "extreme". Returning the only
snapshot as both best and worst would mislead the UI into
rendering identical "best 70.0" / "worst 70.0" badges.

Why dimension→category is hard-coded here: SkillDimension is a
technical metric taxonomy (braking, throttle_pickup, ...);
WeaknessCategory is a domain taxonomy (technique, consistency,
...). The cross-walk is editorial — keeping it explicit (instead
of, say, mining a config table) makes the mapping reviewable in
diffs alongside the rule library.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from lap_schemas.v1 import SkillDrilldown
from lap_schemas.v1.skill_dimension import SkillDimension
from lap_schemas.v1.skill_drilldown import (
    SessionScoreRef,
    SkillDimensionName,
    SkillTrendPoint,
)
from lap_schemas.v1.weakness_episode import WeaknessEpisode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import DriverProgressSnapshot as ProgressSnapshotRow
from infra.orm import User as UserRow
from infra.orm import WeaknessEpisode as WeaknessEpisodeRow

router = APIRouter(prefix="/api/v1/driver", tags=["skills"])

# Maps each SkillDimension field name to its WeaknessCategory.
# Editorial choice — see module docstring.
_DIMENSION_TO_CATEGORY: dict[str, str] = {
    "braking": "technique",
    "throttle_pickup": "technique",
    "minimum_speed": "technique",
    "line_consistency": "consistency",
    "tire_management": "race_management",
    "racecraft": "race_management",
    "fatigue_consistency": "consistency",
    "strategy_execution": "execution",
    "setup_sensitivity": "setup_sensitivity",
    "adaptation": "execution",
}

# Statuses that surface a weakness as actionable in the drilldown.
# Mirrors `_OPEN_RX_STATUSES` in driver_profile but for episodes.
_OPEN_WEAKNESS_STATUSES = ("open", "improving")


@router.get(
    "/skills/{dimension}",
    response_model=SkillDrilldown,
)
async def get_skill_drilldown(
    dimension: SkillDimensionName,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SkillDrilldown:
    driver_id = (
        await db.execute(
            select(DriverProfileRow.id).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if driver_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="driver_not_found"
        )

    snapshots = await _load_snapshots(db, driver_id)
    weaknesses = await _load_related_weaknesses(db, driver_id, dimension)

    return SkillDrilldown(
        dimension=dimension,
        current=_current(snapshots, dimension),
        trend=_trend(snapshots, dimension),
        best_session=_extreme(snapshots, dimension, want_best=True),
        worst_session=_extreme(snapshots, dimension, want_best=False),
        related_weaknesses=weaknesses,
    )


async def _load_snapshots(
    db: AsyncSession, driver_id: UUID
) -> list[ProgressSnapshotRow]:
    """All snapshots for the driver in oldest-first chronological
    order (the natural order for the trend chart)."""
    return list(
        (
            await db.execute(
                select(ProgressSnapshotRow)
                .where(ProgressSnapshotRow.driver_id == driver_id)
                .order_by(ProgressSnapshotRow.captured_at.asc())
            )
        )
        .scalars()
        .all()
    )


def _dim_dict(
    snapshot: ProgressSnapshotRow, dimension: str
) -> dict[str, Any]:
    return getattr(snapshot, dimension)


def _current(
    snapshots: list[ProgressSnapshotRow], dimension: str
) -> SkillDimension | None:
    if not snapshots:
        return None
    # snapshots is asc; last is newest
    return SkillDimension.model_validate(_dim_dict(snapshots[-1], dimension))


def _trend(
    snapshots: list[ProgressSnapshotRow], dimension: str
) -> tuple[SkillTrendPoint, ...]:
    return tuple(
        SkillTrendPoint(
            captured_at=s.captured_at,
            score=float(_dim_dict(s, dimension)["score"]),
            confidence=float(_dim_dict(s, dimension)["confidence"]),
        )
        for s in snapshots
    )


def _extreme(
    snapshots: list[ProgressSnapshotRow],
    dimension: str,
    *,
    want_best: bool,
) -> SessionScoreRef | None:
    """Pick the snapshot with the max (best=True) or min score for
    the dimension. Returns None when there are <2 snapshots — a
    single data point has no meaningful 'extreme' (see docstring)."""
    if len(snapshots) < 2:
        return None
    pick = max if want_best else min
    chosen = pick(snapshots, key=lambda s: float(_dim_dict(s, dimension)["score"]))
    sid_str = _dim_dict(chosen, dimension).get("last_evidence_session_id")
    if sid_str is None:
        return None
    return SessionScoreRef(
        session_id=UUID(sid_str) if isinstance(sid_str, str) else sid_str,
        captured_at=_as_aware(chosen.captured_at),
        score=float(_dim_dict(chosen, dimension)["score"]),
    )


def _as_aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo on round-trip; restore as UTC so the
    schema's timezone-aware datetime accepts the value."""
    from datetime import UTC

    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _load_related_weaknesses(
    db: AsyncSession, driver_id: UUID, dimension: str
) -> tuple[WeaknessEpisode, ...]:
    category = _DIMENSION_TO_CATEGORY[dimension]
    rows = (
        (
            await db.execute(
                select(WeaknessEpisodeRow)
                .where(WeaknessEpisodeRow.driver_id == driver_id)
                .where(WeaknessEpisodeRow.category == category)
                .where(
                    WeaknessEpisodeRow.status.in_(_OPEN_WEAKNESS_STATUSES)
                )
                .order_by(WeaknessEpisodeRow.last_observed_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return tuple(
        WeaknessEpisode.model_validate(
            {
                "id": r.id,
                "driver_id": r.driver_id,
                "category": r.category,
                "subcategory": r.subcategory,
                "corner_tag": r.corner_tag,
                "environment_scope": tuple(r.environment_scope or []),
                "first_observed_at": r.first_observed_at,
                "last_observed_at": r.last_observed_at,
                "recurrence_count": r.recurrence_count,
                "sessions_affected": tuple(r.sessions_affected or []),
                "severity": r.severity,
                "confidence": r.confidence,
                "confidence_decay_per_day": r.confidence_decay_per_day,
                "status": r.status,
                "resolved_at": r.resolved_at,
                "linked_prescriptions": tuple(r.linked_prescriptions or []),
                "linked_insights": tuple(r.linked_insights or []),
                "library_rule_id": r.library_rule_id,
            }
        )
        for r in rows
    )
