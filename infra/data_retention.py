"""T196 — data retention sweep for opted-out drivers.

Drivers with `cohort_opt_in=False` chose not to feed the cohort
benchmark pipeline. Their telemetry session content sticks around
for at most 2 years; older rows have their `manifest` JSON
replaced with a stable anonymized stub so the personally-
identifying lap data no longer persists linked to the driver.

Why scrub the manifest rather than hard-delete? `sessions
.driver_id` is NOT NULL with FK CASCADE — we can't sever the
link without a schema change. Replacing manifest content
achieves the spirit of the policy (no personal lap data
remains), while the row's existence preserves the count
columns that downstream system metrics depend on.

Pure async function, called from infra.celery_app via the
`weekly_data_retention_anonymize` beat task.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import DriverProfile
from infra.orm import Session as SessionRow

# 2 years per the documented policy; the constant is exported so
# tests can pin it and downstream docs/runbooks can reference one
# source of truth.
DEFAULT_RETENTION_DAYS = 730

# The replacement value. Stability matters: re-importers and
# analytics consumers branch on this exact dict shape. A
# regression that changes this marker (e.g. to {"anonymous": True})
# would silently break every downstream tool — the test
# `test_anonymized_manifest_is_stable_marker` pins it.
ANONYMIZED_MANIFEST: dict[str, Any] = {"anonymized": True}


async def anonymize_old_sessions(
    db: AsyncSession,
    *,
    now: datetime,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """Anonymize sessions older than `retention_days` belonging
    to opt-out drivers.

    Args:
        db: open AsyncSession; the function commits.
        now: clock injection — keeps tests deterministic and
            makes "what time is it" explicit at the call site
            (matches the rest of this codebase's cron bodies).
        retention_days: opt-out window. Defaults to 2 years.

    Returns:
        Count of session rows actually scrubbed this run.
        Already-anonymized rows are skipped so a second invocation
        on the same data reports 0 rather than re-counting them
        — keeps the cron log line meaningful for ops.
    """
    cutoff = now - timedelta(days=retention_days)

    rows = (
        (
            await db.execute(
                select(SessionRow)
                .join(
                    DriverProfile,
                    DriverProfile.id == SessionRow.driver_id,
                )
                .where(
                    SessionRow.created_at < cutoff,
                    DriverProfile.cohort_opt_in.is_(False),
                )
            )
        )
        .scalars()
        .all()
    )

    n = 0
    for s in rows:
        if s.manifest != ANONYMIZED_MANIFEST:
            s.manifest = dict(ANONYMIZED_MANIFEST)
            n += 1
    await db.commit()
    return n
