"""T073 add endurance dims to driver_progress_snapshots

Revision ID: a5d3f7c8e2b9
Revises: f4c2d8a5e9b1
Create Date: 2026-04-26 23:00:00.000000

Three nullable JSON columns mirroring the existing per-lap-dim
columns: long_run_consistency, pace_decay, tire_preservation.

Nullable because sprint-only drivers will never have stint data;
additive non-breaking preserves every snapshot already in the DB.

Same JSON-with-JSONB-variant pattern as the existing 10 dim
columns — JSONB on Postgres (indexable/queryable), plain JSON on
SQLite for tests. The values are SkillDimension dicts (score,
confidence, trend_14d, last_evidence_session_id, sample_count).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "a5d3f7c8e2b9"
down_revision: str | Sequence[str] | None = "f4c2d8a5e9b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "driver_progress_snapshots",
        sa.Column(
            "long_run_consistency",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.add_column(
        "driver_progress_snapshots",
        sa.Column(
            "pace_decay",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
    )
    op.add_column(
        "driver_progress_snapshots",
        sa.Column(
            "tire_preservation",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("driver_progress_snapshots", "tire_preservation")
    op.drop_column("driver_progress_snapshots", "pace_decay")
    op.drop_column("driver_progress_snapshots", "long_run_consistency")
