"""T096 add strategy_plans

Revision ID: e3a8c5b9f2d4
Revises: d9f5b1c4e8a3
Create Date: 2026-04-27 02:32:00.000000

StrategyPlan — pre-race strategy intent for one session. One
canonical plan per session (UNIQUE on session_id); the runtime
event log lives in T097's StrategyEvent table. Cascade delete
from sessions: GDPR right-to-erasure on a session purges its
plan with it.

planned_pit_laps as JSON list — cross-dialect (SQLite tests +
Postgres prod) and the column is read whole rather than queried
into, so JSONB's GIN advantage doesn't apply.

No additional indexes; the unique constraint on session_id is
the only read path today and already supplies the index.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "e3a8c5b9f2d4"
down_revision: str | Sequence[str] | None = "d9f5b1c4e8a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_plans",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("planned_stints", sa.Integer(), nullable=False),
        sa.Column(
            "planned_fuel_liters", sa.Float(), nullable=False
        ),
        sa.Column(
            "planned_pit_laps",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            server_default="[]",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", name="uq_strategy_plans_session_id"
        ),
    )


def downgrade() -> None:
    op.drop_table("strategy_plans")
