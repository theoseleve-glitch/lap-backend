"""T008 add action_prescriptions.flagged_wrong_at

Revision ID: f6b9c8d3e1a2
Revises: e5a8b9c2d4f7
Create Date: 2026-04-26 03:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "f6b9c8d3e1a2"
down_revision: str | Sequence[str] | None = "e5a8b9c2d4f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "action_prescriptions",
        sa.Column(
            "flagged_wrong_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("action_prescriptions", "flagged_wrong_at")
