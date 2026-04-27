"""T107 add strategy_templates

Revision ID: b8c5e3f9a1d7
Revises: a7d4f9e2b8c1
Create Date: 2026-04-27 03:55:00.000000

StrategyTemplate — driver-scoped library of named StrategyPlan-
shaped templates the user saves for reuse ("my Le Mans
strategy"). Distinct from T096's per-session StrategyPlan: these
are per-driver, not bound to any session.

UNIQUE(driver_id, name): per-driver discriminability — a user
can't accumulate ambiguous duplicates of their named playbook.
Per-driver, not global — two users can each have a "Spa 24h"
template without conflicting.

Cascade delete from driver_profiles: GDPR right-to-erasure on a
driver purges their template library with them.

planned_pit_laps as JSON list — cross-dialect (SQLite tests +
Postgres prod), read whole rather than queried into.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "b8c5e3f9a1d7"
down_revision: str | Sequence[str] | None = "a7d4f9e2b8c1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("driver_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
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
            ["driver_id"], ["driver_profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "driver_id", "name",
            name="uq_strategy_templates_driver_id_name",
        ),
    )
    op.create_index(
        "ix_strategy_templates_driver_id",
        "strategy_templates",
        ["driver_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_strategy_templates_driver_id",
        table_name="strategy_templates",
    )
    op.drop_table("strategy_templates")
