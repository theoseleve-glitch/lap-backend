"""T054 add cohort_metrics

Revision ID: d2a8b9c4e6f1
Revises: c4f8e2b1d6a9
Create Date: 2026-04-26 14:30:00.000000

Anonymized cohort metric pool (T054). Schema-only task; no
ingestion (that's T055), no compute (T056+), no endpoint (T058).

Privacy invariant: NO driver_id column. Every row is just a
(car, track, metric, value, time) tuple. This is what lets the
percentile compute job join across drivers without pulling
identifying data into the aggregate.

Composite index on (car_id, track_id, metric) is the percentile
query path. session_time is left unindexed for now — we only
filter by recency at the cohort-rebuild boundary, and a separate
btree on session_time would compete with the composite index
without a clear win.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d2a8b9c4e6f1"
down_revision: str | Sequence[str] | None = "c4f8e2b1d6a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "cohort_metrics",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("car_id", sa.Uuid(), nullable=False),
        sa.Column("track_id", sa.Uuid(), nullable=False),
        sa.Column("metric", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column(
            "session_time", sa.DateTime(timezone=True), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_cohort_metrics_car_track_metric",
        "cohort_metrics",
        ["car_id", "track_id", "metric"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cohort_metrics_car_track_metric",
        table_name="cohort_metrics",
    )
    op.drop_table("cohort_metrics")
