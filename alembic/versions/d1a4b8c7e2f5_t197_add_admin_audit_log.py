"""T197 add admin_audit_log table

Revision ID: d1a4b8c7e2f5
Revises: c9f5e3a8b2d6
Create Date: 2026-04-27 15:30:00.000000

Append-only ledger of mutating admin actions. Mirrors the ORM
shape in infra.orm.AdminAuditLog: see that class's docstring
for the field-by-field rationale (especially: no cascade on
admin_user_id — audit history is forever).

Indexes:
  - admin_user_id (filter audit history per admin)
  - action (filter by action type for periodic review)
  - target_id (find every action against a given subject)
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d1a4b8c7e2f5"
down_revision: str | Sequence[str] | None = "c9f5e3a8b2d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "admin_user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id"),
            nullable=False,
            index=True,
        ),
        sa.Column("action", sa.String(64), nullable=False, index=True),
        sa.Column(
            "target_id", sa.Uuid(), nullable=True, index=True
        ),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("admin_audit_log")
