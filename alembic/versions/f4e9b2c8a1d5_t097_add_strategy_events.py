"""T097 add strategy_events

Revision ID: f4e9b2c8a1d5
Revises: e3a8c5b9f2d4
Create Date: 2026-04-27 02:37:00.000000

StrategyEvent — append-only runtime timeline for strategy. One
row per event (plan / armed / pit / stint_end / fcy / sc) with
a JSON details payload and a server-stamped created_at for
same-lap tie-breaking. CASCADE delete from sessions for GDPR.

NOT unique on (session_id, at_lap, kind): two same-kind events
on the same lap are valid (e.g., two FCYs in a chaotic stint).

`kind` enforcement is a Pydantic-side concern (T099+) — DB-level
CHECK would reject the request before the validator that
produces a useful 422.

No additional indexes; the FK index on session_id supports the
only read shape today (load timeline for session X ordered by
lap).
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "f4e9b2c8a1d5"
down_revision: str | Sequence[str] | None = "e3a8c5b9f2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "strategy_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("at_lap", sa.Integer(), nullable=False),
        sa.Column(
            "details",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["session_id"], ["sessions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_strategy_events_session_id",
        "strategy_events",
        ["session_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_strategy_events_session_id", table_name="strategy_events"
    )
    op.drop_table("strategy_events")
