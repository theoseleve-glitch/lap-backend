"""T180 — daily session-loss audit.

A session whose row has been sitting in `recording` status for over
24h is almost certainly a client that crashed mid-upload — the user
won't see their lap data unless we notice. This audit flags those
rows and posts to Slack so an operator can intervene.

Silent on quiet days: no stuck sessions = no Slack post. An alert
channel that pings every morning regardless of state becomes noise
the operator filters out.
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.kpi_report import SlackPoster
from infra.orm import Session as SessionRow

_STUCK_STATUS = "recording"
_STUCK_AGE_THRESHOLD = timedelta(hours=24)


async def audit_stuck_sessions(
    db: AsyncSession,
    *,
    now: datetime,
    threshold: timedelta = _STUCK_AGE_THRESHOLD,
) -> Sequence[SessionRow]:
    """Return Session rows in `recording` older than the threshold.

    `created_at` is the proxy for "when the recording started" —
    we don't track upload progress separately, so any row that
    hasn't transitioned out of `recording` after 24h is presumed
    stuck.
    """
    cutoff = now - threshold
    result = await db.execute(
        select(SessionRow)
        .where(SessionRow.status == _STUCK_STATUS)
        .where(SessionRow.created_at < cutoff)
    )
    return result.scalars().all()


def _format_alert(stuck: Sequence[SessionRow]) -> str:
    count = len(stuck)
    plural = "session" if count == 1 else "sessions"
    return (
        f":warning: Session-loss audit: *{count} stuck {plural}* "
        f"in `recording` >24h. Investigate likely client crashes."
    )


async def send_session_loss_alert(
    db: AsyncSession,
    slack: SlackPoster,
    *,
    channel: str,
    now: datetime,
) -> None:
    """Audit → alert if any. Skips the post entirely on a clean run."""
    stuck = await audit_stuck_sessions(db, now=now)
    if not stuck:
        return
    slack.post(channel, _format_alert(stuck))
