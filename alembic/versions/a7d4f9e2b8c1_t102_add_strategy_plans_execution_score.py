"""T102 add strategy_plans.execution_score

Revision ID: a7d4f9e2b8c1
Revises: f4e9b2c8a1d5
Create Date: 2026-04-27 03:05:00.000000

T102 — 0-100 plan-vs-actual execution score. Null until the
session's strategy data is finalized; T101's divergence
function feeds T102's scoring function which then writes here.

Per-session storage: StrategyPlan is already UNIQUE on
session_id, so attaching the score there keeps it co-located
with the plan it scores against without a separate table.

Nullable + no default: existing rows from before T102 backfill
to NULL ("score not computed yet"), distinct from "score is 0"
(catastrophic divergence).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7d4f9e2b8c1"
down_revision: str | Sequence[str] | None = "f4e9b2c8a1d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "strategy_plans",
        sa.Column("execution_score", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("strategy_plans", "execution_score")
