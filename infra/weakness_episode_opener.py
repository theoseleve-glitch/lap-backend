"""T208 — open or merge a WeaknessEpisode for a CoachingInsight.

A WeaknessEpisode (ADR-005 §5.2) is the longitudinal record
("the driver struggles with X") that owns N CoachingInsights
("on session Y, rule X fired"). This module is the dedup link:
when a fresh insight lands, find the OPEN episode for the same
(driver_id, library_rule_id, corner_tag) tuple — merge in if it
exists, open new otherwise.

Resolved episodes are NOT candidates for merge: a fresh
occurrence after resolution is a regression worth its own
episode, not a stealth reopen of last quarter's record.

Initial confidence is 1.0 — the rule fired, we know the weakness
exists right now. The decay job (T209) will lower this over time
without re-observation.
"""

from datetime import datetime
from uuid import UUID

import uuid6
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from coaching.library.rule_schema import RuleSpec
from infra.orm import CoachingInsight as CoachingInsightRow
from infra.orm import WeaknessEpisode

_INITIAL_CONFIDENCE = 1.0
_INITIAL_RECURRENCE = 1
_INITIAL_STATUS = "open"


async def upsert_weakness_episode(
    db: AsyncSession,
    *,
    insight_row: CoachingInsightRow,
    rule: RuleSpec,
    now: datetime,
) -> tuple[UUID, bool]:
    """Find an OPEN WeaknessEpisode matching (driver_id,
    library_rule_id, corner_tag) and merge into it; otherwise
    open a new one. Returns (episode_id, opened_new) — True iff
    a fresh row was inserted."""
    stmt = (
        select(WeaknessEpisode)
        .where(WeaknessEpisode.driver_id == insight_row.driver_id)
        .where(WeaknessEpisode.library_rule_id == insight_row.rule_id)
        .where(WeaknessEpisode.status == _INITIAL_STATUS)
    )
    if insight_row.corner_tag is None:
        stmt = stmt.where(WeaknessEpisode.corner_tag.is_(None))
    else:
        stmt = stmt.where(
            WeaknessEpisode.corner_tag == insight_row.corner_tag
        )
    existing = (await db.execute(stmt)).scalar_one_or_none()

    if existing is not None:
        # T210 — recurrence = a new occurrence in TIME, not a
        # re-process of the same session. Celery retries land
        # here with a fresh insight row but a session_id that's
        # already in sessions_affected — must not double-count.
        # linked_insights still appends so the audit trail stays
        # complete (insight rows are 1:1 with persist runs).
        session_str = str(insight_row.session_id)
        is_new_session = session_str not in existing.sessions_affected
        if is_new_session:
            existing.recurrence_count += 1
            existing.last_observed_at = now
            existing.sessions_affected = [
                *existing.sessions_affected,
                session_str,
            ]
        existing.linked_insights = [
            *existing.linked_insights,
            str(insight_row.id),
        ]
        await db.commit()
        return existing.id, False

    new_id = uuid6.uuid7()
    db.add(
        WeaknessEpisode(
            id=new_id,
            driver_id=insight_row.driver_id,
            category=rule.category,
            subcategory=rule.subcategory,
            corner_tag=insight_row.corner_tag,
            environment_scope=[],
            first_observed_at=now,
            last_observed_at=now,
            recurrence_count=_INITIAL_RECURRENCE,
            sessions_affected=[str(insight_row.session_id)],
            severity=insight_row.severity,
            confidence=_INITIAL_CONFIDENCE,
            status=_INITIAL_STATUS,
            linked_prescriptions=[],
            linked_insights=[str(insight_row.id)],
            library_rule_id=insight_row.rule_id,
        )
    )
    await db.commit()
    return new_id, True
