"""T164 add organization_invitations

Revision ID: e5b9f2c1d8a4
Revises: d4e8a1c9b6f3
Create Date: 2026-04-27 10:18:00.000000

OrganizationInvitation — pending invitation issued by an org
admin to bring a new user (by email) into the org under a role.

`token` is the opaque redemption credential (T165's accept
endpoint consumes it). UNIQUE on token to prevent collisions.

`expires_at` enforces 7-day TTL — invitations stale silently
rather than living forever. Implicit status: accepted_at NULL
AND expires_at > now() = pending; accepted_at != NULL =
accepted; accepted_at NULL AND expires_at <= now() = expired.

Cascade on org_id: deleting an org cleans up outstanding
invitations along with members. No FK on email (the invitee
may not have a user row yet — they're invited BY email).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e5b9f2c1d8a4"
down_revision: str | Sequence[str] | None = "d4e8a1c9b6f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "organization_invitations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=False
        ),
        sa.Column(
            "accepted_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "token", name="uq_organization_invitations_token"
        ),
    )
    op.create_index(
        "ix_organization_invitations_org_id",
        "organization_invitations",
        ["org_id"],
    )
    op.create_index(
        "ix_organization_invitations_email",
        "organization_invitations",
        ["email"],
    )
    op.create_index(
        "ix_organization_invitations_token",
        "organization_invitations",
        ["token"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_organization_invitations_token",
        table_name="organization_invitations",
    )
    op.drop_index(
        "ix_organization_invitations_email",
        table_name="organization_invitations",
    )
    op.drop_index(
        "ix_organization_invitations_org_id",
        table_name="organization_invitations",
    )
    op.drop_table("organization_invitations")
