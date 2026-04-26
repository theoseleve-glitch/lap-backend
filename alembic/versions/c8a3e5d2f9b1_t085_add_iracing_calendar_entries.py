"""T085 add iracing_calendar_entries

Revision ID: c8a3e5d2f9b1
Revises: b7e2c4a8f1d3
Create Date: 2026-04-27 02:00:00.000000

iRacing calendar storage (T085). Populated by the weekly cron
landing in T087; consumed by the T086 parser and the target-
event suggestion algorithm in T092.

source_id is iRacing's own identifier — stable across re-fetches,
indexed UNIQUE so the scraper's upsert path can match in O(log n)
without a full scan.

track_name and car_class are nullable free-text. T086 will resolve
track_name to a TrackLayout where possible, but new tracks not yet
in our catalog still get recorded — we don't drop rows just because
the catalog lag exists.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8a3e5d2f9b1"
down_revision: str | Sequence[str] | None = "b7e2c4a8f1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "iracing_calendar_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_id", sa.String(length=128), nullable=False),
        sa.Column("series_name", sa.String(length=255), nullable=False),
        sa.Column("event_name", sa.String(length=255), nullable=False),
        sa.Column(
            "starts_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "ends_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "track_name", sa.String(length=255), nullable=True
        ),
        sa.Column(
            "car_class", sa.String(length=128), nullable=True
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "fetched_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_iracing_calendar_entries_source_id",
        "iracing_calendar_entries",
        ["source_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_iracing_calendar_entries_source_id",
        table_name="iracing_calendar_entries",
    )
    op.drop_table("iracing_calendar_entries")
