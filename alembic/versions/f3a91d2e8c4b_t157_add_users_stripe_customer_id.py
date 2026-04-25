"""T157 add users.stripe_customer_id

Revision ID: f3a91d2e8c4b
Revises: c1f0a4b8d2e7
Create Date: 2026-04-25 18:30:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "f3a91d2e8c4b"
down_revision: str | Sequence[str] | None = "c1f0a4b8d2e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("stripe_customer_id", sa.String(64), nullable=True),
    )
    op.create_index(
        "ix_users_stripe_customer_id",
        "users",
        ["stripe_customer_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_users_stripe_customer_id", table_name="users")
    op.drop_column("users", "stripe_customer_id")
