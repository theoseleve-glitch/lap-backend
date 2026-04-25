"""T163 add users.trial_ended_at

Revision ID: c2b8e1a4d7f3
Revises: b9e4d12f3a8c
Create Date: 2026-04-25 21:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "c2b8e1a4d7f3"
down_revision: str | Sequence[str] | None = "b9e4d12f3a8c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "trial_ended_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "trial_ended_at")
