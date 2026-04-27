"""T094 add target_events.notifications_sent

Revision ID: d9f5b1c4e8a3
Revises: c8a3e5d2f9b1
Create Date: 2026-04-27 02:18:00.000000

T094 — per-event idempotency tracking for the 24h/1h email
notifications. JSON array of window labels already dispatched
(e.g. ["24h"] after the T-24h reminder fires). The hourly
Celery beat filters on this so retries and clock-skew "still
in window" ticks don't double-send.

JSON column with default `[]` (server_default plus client
default) — existing rows backfill to empty so the next beat
treats them as never-notified, which is the safe default
(slightly redundant first reminder vs missed reminder).

JSON over JSONB at the column level: cross-dialect portability
(SQLite tests + Postgres prod), and the column is read whole
per-event (not queried into) so JSONB's GIN advantage doesn't
apply.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "d9f5b1c4e8a3"
down_revision: str | Sequence[str] | None = "c8a3e5d2f9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "target_events",
        sa.Column(
            "notifications_sent",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("target_events", "notifications_sent")
