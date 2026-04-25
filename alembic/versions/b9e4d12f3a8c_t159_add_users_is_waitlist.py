"""T159 add users.is_waitlist

Revision ID: b9e4d12f3a8c
Revises: f3a91d2e8c4b
Create Date: 2026-04-25 19:00:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "b9e4d12f3a8c"
down_revision: str | Sequence[str] | None = "f3a91d2e8c4b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_waitlist",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_waitlist")
