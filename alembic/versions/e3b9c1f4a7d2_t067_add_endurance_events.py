"""T067 add endurance_events

Revision ID: e3b9c1f4a7d2
Revises: d2a8b9c4e6f1
Create Date: 2026-04-26 21:55:00.000000

EnduranceEvent aggregate (T067). Schema-only task; entity row
population (T068+ stint detection) and read endpoints (T075+)
land in later tasks.

driver_ids is a JSON list[UUID-string]. JSONB on Postgres,
plain JSON on SQLite — same with_variant pattern used by
sessions.manifest. Stored as strings rather than UUIDs because
JSON has no native UUID type and round-tripping through Python's
JSON encoder requires a string anyway.

No indexes yet: read patterns will materialize in T075/T076
(per-event lookups, driver-history scans). Adding speculative
indexes now would commit to a query shape the consumers haven't
defined.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "e3b9c1f4a7d2"
down_revision: str | Sequence[str] | None = "d2a8b9c4e6f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "endurance_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("event_name", sa.String(length=255), nullable=False),
        sa.Column(
            "total_duration_seconds", sa.Float(), nullable=False
        ),
        sa.Column("stint_count", sa.Integer(), nullable=False),
        sa.Column(
            "driver_ids",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("endurance_events")
