"""T207 — bridge `run_rules` dataclass output to coaching_insights.

Lives in `infra/` (not `coaching/`) so the rules library keeps
zero SQLAlchemy dependency. The Celery task that runs rules at
session-ready time will call `persist_insights(db, insights=...,
session_id=..., driver_id=..., now=...)` after consuming the
runner's output.

Defaults are the cautious ones: severity='medium' and
confidence=1.0 (rule fired = 100% confident in the detection;
the post-detection confidence_gate from T187 is independent).
The runtime dataclass doesn't carry severity yet; T-future can
extend the runner to compute one from `severity_rule` and pass
it through.
"""

from collections.abc import Iterable
from datetime import datetime
from uuid import UUID

import uuid6
from sqlalchemy.ext.asyncio import AsyncSession

from coaching.library.rule_runner import CoachingInsight as RunnerInsight
from infra.orm import CoachingInsight as CoachingInsightRow

_DEFAULT_SEVERITY = "medium"
_DEFAULT_CONFIDENCE = 1.0


async def persist_insights(
    db: AsyncSession,
    *,
    insights: Iterable[RunnerInsight],
    session_id: UUID,
    driver_id: UUID,
    now: datetime,
) -> list[UUID]:
    """Persist runner insights to coaching_insights. Returns the
    inserted row IDs in the same order as the input — callers
    can correlate index-wise (e.g., when emitting analytics
    events alongside DB writes)."""
    rows: list[CoachingInsightRow] = []
    for ins in insights:
        rows.append(
            CoachingInsightRow(
                id=uuid6.uuid7(),
                session_id=session_id,
                driver_id=driver_id,
                created_at=now,
                rule_id=ins.rule_id,
                rule_version=str(ins.rule_version),
                observation=ins.explanation,
                evidence=[],
                severity=_DEFAULT_SEVERITY,
                confidence=_DEFAULT_CONFIDENCE,
            )
        )
    if not rows:
        return []
    db.add_all(rows)
    await db.commit()
    return [r.id for r in rows]
