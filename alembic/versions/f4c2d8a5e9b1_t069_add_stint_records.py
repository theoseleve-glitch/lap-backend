"""T069 add stint_records

Revision ID: f4c2d8a5e9b1
Revises: e3b9c1f4a7d2
Create Date: 2026-04-26 22:30:00.000000

StintRecord — per-stint persistence for endurance sessions.
T068 detects boundaries, T070-T072 derive the per-stint metrics
(consistency, pace decay, tire preservation), T069 stores them.

CASCADE on session_id: when a session is purged (GDPR right-to-
erasure), its stints go too. EnduranceEvent itself survives.

Unique (session_id, stint_index): a re-fire of the stint-detection
job must not silently double-write rows and inflate counts on the
EnduranceEvent rollup.

Index on session_id (implicit via FK) supports the per-session
read path used by T075 (event summary endpoint). No additional
indexes — the read patterns for fuel/decay/tire are still being
defined in T077+ and adding speculative indexes now would commit
to a query shape that hasn't materialized.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f4c2d8a5e9b1"
down_revision: str | Sequence[str] | None = "e3b9c1f4a7d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stint_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("stint_index", sa.Integer(), nullable=False),
        sa.Column("start_lap", sa.Integer(), nullable=False),
        sa.Column("end_lap", sa.Integer(), nullable=False),
        sa.Column("lap_count", sa.Integer(), nullable=False),
        sa.Column("tire_age_laps", sa.Integer(), nullable=False),
        sa.Column("fuel_used_liters", sa.Float(), nullable=False),
        sa.Column(
            "lap_time_decay_seconds", sa.Float(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "stint_index",
            name="uq_stint_records_session_index",
        ),
    )
    op.create_index(
        "ix_stint_records_session_id",
        "stint_records",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_stint_records_session_id", table_name="stint_records"
    )
    op.drop_table("stint_records")
