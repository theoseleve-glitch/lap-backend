"""T179 — weekly KPI report (Slack delivery + cron).

Single source of truth for the three operator KPIs (WPD, activation,
upload success). The same `compute_weekly_kpis` powers T178's admin
dashboard and the Monday-morning Slack post — duplicating the SQL
would let the two callers drift silently.

`SlackPoster` is a Protocol so tests inject an in-memory recorder
and prod wires `slack_sdk.WebClient.chat_postMessage`.
"""

from datetime import datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from infra.orm import DriverProfile as DriverProfileRow
from infra.orm import Session as SessionRow
from infra.orm import User as UserRow

_KPI_WINDOW = timedelta(days=7)
_PAID_STATUSES = ("active", "trialing")
_READY_SESSION_STATUS = "ready"


class SlackPoster(Protocol):
    def post(self, channel: str, text: str) -> None: ...


async def compute_weekly_kpis(
    db: AsyncSession, now: datetime
) -> dict[str, Any]:
    """Return WPD, activation rate, upload success rate over last 7d.

    Mirrors `app/routers/admin.py::admin_kpis` exactly — both call
    here so the dashboard and the Slack message can never disagree.
    """
    threshold = now - _KPI_WINDOW

    wpd = (
        await db.execute(
            select(func.count(func.distinct(UserRow.id)))
            .join(
                DriverProfileRow,
                DriverProfileRow.user_id == UserRow.id,
            )
            .join(
                SessionRow,
                SessionRow.driver_id == DriverProfileRow.id,
            )
            .where(UserRow.subscription_status.in_(_PAID_STATUSES))
            .where(SessionRow.created_at >= threshold)
        )
    ).scalar_one()

    total_signups = (
        await db.execute(
            select(func.count(UserRow.id)).where(
                UserRow.created_at >= threshold
            )
        )
    ).scalar_one()
    activated_signups = (
        await db.execute(
            select(func.count(func.distinct(UserRow.id)))
            .join(
                DriverProfileRow,
                DriverProfileRow.user_id == UserRow.id,
            )
            .join(
                SessionRow,
                SessionRow.driver_id == DriverProfileRow.id,
            )
            .where(UserRow.created_at >= threshold)
        )
    ).scalar_one()
    activation_rate = (
        activated_signups / total_signups if total_signups > 0 else 0.0
    )

    total_sessions = (
        await db.execute(
            select(func.count(SessionRow.id)).where(
                SessionRow.created_at >= threshold
            )
        )
    ).scalar_one()
    ready_sessions = (
        await db.execute(
            select(func.count(SessionRow.id))
            .where(SessionRow.created_at >= threshold)
            .where(SessionRow.status == _READY_SESSION_STATUS)
        )
    ).scalar_one()
    upload_success_rate = (
        ready_sessions / total_sessions if total_sessions > 0 else 1.0
    )

    return {
        "wpd": int(wpd),
        "activation_rate": float(activation_rate),
        "upload_success_rate": float(upload_success_rate),
    }


def format_weekly_report(
    kpis: dict[str, Any], week_ending: datetime
) -> str:
    """Format the three KPIs as a one-glance Slack message."""
    wpd = int(kpis["wpd"])
    activation_pct = round(float(kpis["activation_rate"]) * 100, 1)
    upload_pct = round(float(kpis["upload_success_rate"]) * 100, 1)
    week_label = week_ending.date().isoformat()
    return (
        f"*Weekly KPI Report — week ending {week_label}*\n"
        f"• WPD (Weekly Paid Drivers): {wpd}\n"
        f"• Activation rate: {activation_pct}%\n"
        f"• Upload success rate: {upload_pct}%"
    )


async def send_weekly_kpi_report(
    db: AsyncSession,
    slack: SlackPoster,
    *,
    channel: str,
    now: datetime,
) -> None:
    """Compute → format → post. The cron task wires this up."""
    kpis = await compute_weekly_kpis(db, now)
    text = format_weekly_report(kpis, now)
    slack.post(channel, text)
