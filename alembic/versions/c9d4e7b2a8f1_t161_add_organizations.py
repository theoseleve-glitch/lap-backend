"""T161 add organizations

Revision ID: c9d4e7b2a8f1
Revises: b8c5e3f9a1d7
Create Date: 2026-04-27 09:55:00.000000

Organization — org-scoped container for team / fleet / coaching-
academy use cases. The `admin_user_id` FK → users.id is the org
admin (typically the creator). No cascade on the FK: deleting
the admin user must fail loudly rather than silently orphan the
org's members and billing record.

`plan` defaults to "free" both client-side (ORM default) and
server-side (server_default) so a freshly-inserted org from raw
SQL also lands on the conservative tier. `seats_limit` is
required — there is no sensible default (free=1, team=10+,
pro=50+ depend on the plan), so callers must specify it.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c9d4e7b2a8f1"
down_revision: str | Sequence[str] | None = "b8c5e3f9a1d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organizations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column(
            "plan",
            sa.String(length=32),
            server_default="free",
            nullable=False,
        ),
        sa.Column("admin_user_id", sa.Uuid(), nullable=False),
        sa.Column("seats_limit", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["admin_user_id"], ["users.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_organizations_admin_user_id",
        "organizations",
        ["admin_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organizations_admin_user_id",
        table_name="organizations",
    )
    op.drop_table("organizations")
