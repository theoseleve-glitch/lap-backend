"""T007 (sprint2-backend) — GET /api/v1/driver/weaknesses/{id}.

Returns a WeaknessDetail composite: the episode + history (per-
session timeline) + evidence (linked CoachingInsights) +
linked_prescriptions.

Cross-driver isolation: 404 (not 403) for absent OR not-owned
weakness ids — same convention as compare_cases / sessions.

The history is derived from `WeaknessEpisode.sessions_affected`
joined to the Sessions table for `created_at` — clients render a
timeline without a per-row /sessions/{id} round-trip.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from lap_schemas.v1 import WeaknessDetail, WeaknessHistoryEntry
from lap_schemas.v1.action_prescription import ActionPrescriptionListItem
from lap_schemas.v1.coaching_insight import CoachingInsight
from lap_schemas.v1.weakness_episode import WeaknessEpisode
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.deps import get_current_user, get_db
from infra.orm import ActionPrescription as ActionPrescriptionRow
from infra.orm import CoachingInsight as CoachingInsightRow
from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import Session as SessionRow
from infra.orm import User as UserRow
from infra.orm import WeaknessEpisode as WeaknessEpisodeRow

router = APIRouter(prefix="/api/v1/driver", tags=["weaknesses"])


@router.get("/weaknesses/{weakness_id}", response_model=WeaknessDetail)
async def get_weakness_detail(
    weakness_id: UUID,
    user: Annotated[UserRow, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WeaknessDetail:
    driver_id = (
        await db.execute(
            select(DriverProfileRow.id).where(
                DriverProfileRow.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if driver_id is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="weakness not found"
        )

    row = (
        await db.execute(
            select(WeaknessEpisodeRow).where(
                WeaknessEpisodeRow.id == weakness_id
            )
        )
    ).scalar_one_or_none()
    if row is None or row.driver_id != driver_id:
        # 404 (not 403) so the caller can't enumerate ids they don't own.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="weakness not found"
        )

    return WeaknessDetail(
        weakness=_episode_schema(row),
        history=await _build_history(db, row.sessions_affected),
        evidence=await _load_insights(db, row.linked_insights),
        linked_prescriptions=await _load_prescriptions(
            db, row.linked_prescriptions
        ),
    )


async def _build_history(
    db: AsyncSession, session_id_strings: list[str]
) -> list[WeaknessHistoryEntry]:
    if not session_id_strings:
        return []
    uuids = [UUID(s) for s in session_id_strings]
    rows = (
        (
            await db.execute(
                select(SessionRow.id, SessionRow.created_at).where(
                    SessionRow.id.in_(uuids)
                )
            )
        )
        .all()
    )
    return [
        WeaknessHistoryEntry(session_id=sid, session_created_at=created_at)
        for sid, created_at in rows
    ]


async def _load_insights(
    db: AsyncSession, insight_id_strings: list[str]
) -> list[CoachingInsight]:
    if not insight_id_strings:
        return []
    uuids = [UUID(s) for s in insight_id_strings]
    rows = (
        (
            await db.execute(
                select(CoachingInsightRow).where(
                    CoachingInsightRow.id.in_(uuids)
                )
            )
        )
        .scalars()
        .all()
    )
    return [
        CoachingInsight(
            id=r.id,
            session_id=r.session_id,
            driver_id=r.driver_id,
            created_at=r.created_at,
            rule_id=r.rule_id,
            rule_version=r.rule_version,
            observation=r.observation,
            evidence=tuple(r.evidence or []),
            likely_cause=r.likely_cause,
            severity=r.severity,
            confidence=r.confidence,
            corner_tag=r.corner_tag,
            suggested_prescription_id=r.suggested_prescription_id,
            flagged_false_positive=r.flagged_false_positive,
        )
        for r in rows
    ]


async def _load_prescriptions(
    db: AsyncSession, rx_id_strings: list[str]
) -> list[ActionPrescriptionListItem]:
    if not rx_id_strings:
        return []
    uuids = [UUID(s) for s in rx_id_strings]
    rows = (
        (
            await db.execute(
                select(ActionPrescriptionRow).where(
                    ActionPrescriptionRow.id.in_(uuids)
                )
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


def _episode_schema(row: WeaknessEpisodeRow) -> WeaknessEpisode:
    payload: dict[str, Any] = {
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
    return WeaknessEpisode.model_validate(payload)
