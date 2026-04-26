"""T004 (sprint2-backend) — GET /api/v1/driver/profile aggregate.

Single-shot composite that surfaces what a driver-home view
needs in one round-trip: latest progress snapshot, top-5
weakness episodes (severity-ordered), active prescriptions,
and the latest readiness state.

Why one endpoint, not four: the doctrine is "server owns the
brain, clients stay thin". Four separate calls means four
JWT verifies, four DB connections, four round-trips of
network latency stacked on the cold-cache phone client. One
aggregate is faster, simpler, and the client's UI is one
component (the home screen) so coupling them server-side
matches the user-perceived unit.

Latency budget: <200ms p95 (T004 sprint contract; load test
in scripts/k6/driver_profile.js, which today targets /me but
should be extended to this endpoint when v2 ships).

Severity ordering for top-5 uses a static mapping (high>medium>low)
because the schema's Literal is closed-set and ordering by
severity-string lexically would put "high" < "medium" < "low" —
correct only by accident.
"""

from __future__ import annotations

from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from lap_schemas.v1 import DriverProfile as DriverProfileSchema
from lap_schemas.v1 import DriverProfileAggregate, DriverProfileUpdate
from lap_schemas.v1.action_prescription import ActionPrescriptionListItem
from lap_schemas.v1.driver_profile import SkillLevel
from lap_schemas.v1.driver_progress_snapshot import DriverProgressSnapshot
from lap_schemas.v1.driver_readiness_state import DriverReadinessState
from lap_schemas.v1.weakness_episode import WeaknessEpisode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import ActionPrescription as ActionPrescriptionRow
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import DriverProgressSnapshot as ProgressSnapshotRow
from infra.orm import DriverReadinessState as ReadinessStateRow
from infra.orm import User as UserRow
from infra.orm import WeaknessEpisode as WeaknessEpisodeRow

router = APIRouter(prefix="/api/v1/driver", tags=["driver-profile"])

_TOP_WEAKNESSES = 5
_OPEN_RX_STATUSES = ("proposed", "accepted", "in_progress")
# severity rank: higher number = more severe → sort key reverse=True
_SEVERITY_RANK = {"low": 0, "medium": 1, "high": 2}


# T053 (sprint2-backend) — PATCH /api/v1/driver/profile.
# Update-only endpoint for the authenticated driver's profile;
# the focus is the cohort_opt_in toggle (sprint requirement) but
# the body accepts the full DriverProfileUpdate so the UI can
# patch any subset of fields without choosing between endpoints.
# Bootstrap-create remains on PATCH /me — this endpoint 404s on
# missing profile.
@router.patch(
    "/profile",
    response_model=DriverProfileSchema,
    responses={404: {"description": "driver_not_found"}},
)
async def patch_driver_profile(
    update: DriverProfileUpdate,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DriverProfileSchema:
    profile = (
        await db.execute(
            select(DriverProfileRow).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="driver_not_found",
        )
    if update.display_name is not None:
        profile.display_name = update.display_name
    if update.skill_level is not None:
        profile.skill_level = update.skill_level
    if update.cohort_opt_in is not None:
        profile.cohort_opt_in = update.cohort_opt_in
    await db.commit()
    return DriverProfileSchema(
        id=profile.id,
        user_id=profile.user_id,
        display_name=profile.display_name,
        skill_level=cast(SkillLevel, profile.skill_level),
        cohort_opt_in=profile.cohort_opt_in,
    )


@router.get("/profile", response_model=DriverProfileAggregate)
async def get_driver_profile_aggregate(
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DriverProfileAggregate:
    driver_id = (
        await db.execute(
            select(DriverProfileRow.id).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if driver_id is None:
        return DriverProfileAggregate()

    progress = await _latest_progress(db, driver_id)
    weaknesses = await _top_weaknesses(db, driver_id)
    prescriptions = await _active_prescriptions(db, driver_id)
    readiness = await _latest_readiness(db, driver_id)
    return DriverProfileAggregate(
        progress=progress,
        top_weaknesses=weaknesses,
        active_prescriptions=prescriptions,
        readiness=readiness,
    )


async def _latest_progress(
    db: AsyncSession, driver_id: UUID
) -> DriverProgressSnapshot | None:
    row = (
        await db.execute(
            select(ProgressSnapshotRow)
            .where(ProgressSnapshotRow.driver_id == driver_id)
            .order_by(ProgressSnapshotRow.captured_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return DriverProgressSnapshot.model_validate(_progress_dict(row))


async def _top_weaknesses(
    db: AsyncSession, driver_id: UUID
) -> list[WeaknessEpisode]:
    # Fetch ALL weaknesses for the driver, then sort severity-DESC
    # in Python — the severity rank isn't lexical so a SQL ORDER BY
    # on the string would be wrong, and Python sort over typically
    # tens of episodes is cheap. Future T can add a CASE-WHEN if
    # drivers ever accumulate hundreds of open weaknesses.
    rows = (
        (
            await db.execute(
                select(WeaknessEpisodeRow).where(
                    WeaknessEpisodeRow.driver_id == driver_id
                )
            )
        )
        .scalars()
        .all()
    )
    rows = sorted(
        rows,
        key=lambda r: (
            _SEVERITY_RANK.get(r.severity, -1),
            r.last_observed_at,
        ),
        reverse=True,
    )[:_TOP_WEAKNESSES]
    return [WeaknessEpisode.model_validate(_weakness_dict(r)) for r in rows]


async def _active_prescriptions(
    db: AsyncSession, driver_id: UUID
) -> list[ActionPrescriptionListItem]:
    rows = (
        (
            await db.execute(
                select(ActionPrescriptionRow)
                .where(ActionPrescriptionRow.driver_id == driver_id)
                .where(
                    ActionPrescriptionRow.status.in_(_OPEN_RX_STATUSES)
                )
                .order_by(ActionPrescriptionRow.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return [
        ActionPrescriptionListItem(
            id=r.id,
            driver_id=r.driver_id,
            weakness_episode_id=r.weakness_episode_id,
            created_at=r.created_at,
            priority=r.priority,
            title=r.title,
            rationale=r.rationale,
            drill_template_id=r.drill_template_id,
            success_condition=r.success_condition,
            status=r.status,
            accepted_at=r.accepted_at,
        )
        for r in rows
    ]


async def _latest_readiness(
    db: AsyncSession, driver_id: UUID
) -> DriverReadinessState | None:
    row = (
        await db.execute(
            select(ReadinessStateRow)
            .where(ReadinessStateRow.driver_id == driver_id)
            .order_by(ReadinessStateRow.computed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return DriverReadinessState.model_validate(_readiness_dict(row))


# -- ORM-row → schema dict converters --------------------------


def _progress_dict(row: ProgressSnapshotRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "driver_id": row.driver_id,
        "captured_at": row.captured_at,
        "window": row.window,
        "window_start": row.window_start,
        "window_end": row.window_end,
        "braking": row.braking,
        "throttle_pickup": row.throttle_pickup,
        "minimum_speed": row.minimum_speed,
        "line_consistency": row.line_consistency,
        "tire_management": row.tire_management,
        "racecraft": row.racecraft,
        "fatigue_consistency": row.fatigue_consistency,
        "strategy_execution": row.strategy_execution,
        "setup_sensitivity": row.setup_sensitivity,
        "adaptation": row.adaptation,
        "overall_score": row.overall_score,
        "overall_confidence": row.overall_confidence,
        "percentile_vs_cohort": row.percentile_vs_cohort,
        "generated_by": row.generated_by,
    }


def _weakness_dict(row: WeaknessEpisodeRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "driver_id": row.driver_id,
        "category": row.category,
        "subcategory": row.subcategory,
        "corner_tag": row.corner_tag,
        "environment_scope": tuple(row.environment_scope or []),
        "first_observed_at": row.first_observed_at,
        "last_observed_at": row.last_observed_at,
        "recurrence_count": row.recurrence_count,
        "sessions_affected": tuple(row.sessions_affected or []),
        "severity": row.severity,
        "confidence": row.confidence,
        "confidence_decay_per_day": row.confidence_decay_per_day,
        "status": row.status,
        "resolved_at": row.resolved_at,
        "linked_prescriptions": tuple(row.linked_prescriptions or []),
        "linked_insights": tuple(row.linked_insights or []),
        "library_rule_id": row.library_rule_id,
    }


def _readiness_dict(row: ReadinessStateRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "driver_id": row.driver_id,
        "target_event": row.target_event,
        "computed_at": row.computed_at,
        "readiness_score": row.readiness_score,
        "volume_input": row.volume_input,
        "quality_input": row.quality_input,
        "recency_input": row.recency_input,
        "consistency_input": row.consistency_input,
        "unresolved_weaknesses_input": row.unresolved_weaknesses_input,
        "familiarity_input": row.familiarity_input,
        "event_pressure_input": row.event_pressure_input,
        "recovery_input": row.recovery_input,
        "limiting_factor": row.limiting_factor,
        "likely_gain_area": row.likely_gain_area,
        "recommended_sessions_before_event": row.recommended_sessions_before_event,
    }
