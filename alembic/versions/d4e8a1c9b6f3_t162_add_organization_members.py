"""T162 add organization_members

Revision ID: d4e8a1c9b6f3
Revises: c9d4e7b2a8f1
Create Date: 2026-04-27 10:05:00.000000

OrganizationMember — join table between Organization (T161) and
User with role information. A user can hold one role per org;
re-roling goes through the role-update path (T164/T165), not
via duplicate inserts.

`role` is String(32) — the {admin, coach, driver} enum is
enforced upstream at the endpoint layer, not via DB CHECK,
consistent with the codebase convention for role/status
columns (User.subscription_status etc).

Cascades:
  - org_id ON DELETE CASCADE — deleting an org cleans up its
    member rows.
  - user_id ON DELETE CASCADE — GDPR right-to-erasure on a user
    purges their org memberships with them.

UNIQUE(org_id, user_id) — one role per user per org.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d4e8a1c9b6f3"
down_revision: str | Sequence[str] | None = "c9d4e7b2a8f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_members",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "user_id",
            name="uq_organization_members_org_id_user_id",
        ),
    )
    op.create_index(
        "ix_organization_members_org_id",
        "organization_members",
        ["org_id"],
    )
    op.create_index(
        "ix_organization_members_user_id",
        "organization_members",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_members_user_id",
        table_name="organization_members",
    )
    op.drop_index(
        "ix_organization_members_org_id",
        table_name="organization_members",
    )
    op.drop_table("organization_members")
