"""T081 add target_events

Revision ID: b7e2c4a8f1d3
Revises: a5d3f7c8e2b9
Create Date: 2026-04-27 00:30:00.000000

TargetEvent (T081). Persistence for driver-pinned upcoming sim
events and real-life sessions per ADR §5.7. Schema-only task;
the POST/DELETE/GET endpoints land in T082-T084 and the
readiness recomputation hook lands in T089.

driver_id uses ON DELETE CASCADE: a purged driver loses their
pinned targets too (GDPR right-to-erasure).

track_id and car_id use ON DELETE SET NULL: catalog rows can
churn (consolidations, master_id remaps); a pinned target
should survive the catalog change as a free-text reference.

No indexes beyond the driver_id FK index — query shapes for
T084 (per-driver list) and T089 (readiness recompute) will be
visible when those tasks land. Speculative indexes now would
commit to a shape the consumers haven't defined.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7e2c4a8f1d3"
down_revision: str | Sequence[str] | None = "a5d3f7c8e2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "target_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("driver_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column(
            "starts_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "ends_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("track_id", sa.Uuid(), nullable=True),
        sa.Column("car_id", sa.Uuid(), nullable=True),
        sa.Column("competitive_weight", sa.Float(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["driver_id"], ["driver_profiles.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["track_id"], ["track_layouts.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["car_id"], ["car_models.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_target_events_driver_id",
        "target_events",
        ["driver_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_target_events_driver_id", table_name="target_events")
    op.drop_table("target_events")
