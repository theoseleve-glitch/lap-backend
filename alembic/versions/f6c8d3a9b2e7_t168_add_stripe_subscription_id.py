"""T168 add organizations.stripe_subscription_id

Revision ID: f6c8d3a9b2e7
Revises: e5b9f2c1d8a4
Create Date: 2026-04-27 11:00:00.000000

Adds a nullable `stripe_subscription_id` column to the
`organizations` table. NULL = free tier (no Stripe sync);
set = the org's seat count is synced to Stripe's subscription
quantity on member add / remove with proration.

Nullable because the free tier exists and free orgs aren't
billed. NOT NULL would force every org to be billable.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f6c8d3a9b2e7"
down_revision: str | Sequence[str] | None = "e5b9f2c1d8a4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "stripe_subscription_id",
            sa.String(length=64),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "stripe_subscription_id")
