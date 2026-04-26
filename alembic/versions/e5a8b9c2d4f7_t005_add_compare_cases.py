"""T005 add compare_cases

Revision ID: e5a8b9c2d4f7
Revises: c2b8e1a4d7f3
Create Date: 2026-04-26 03:15:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


revision: str = "e5a8b9c2d4f7"
down_revision: str | Sequence[str] | None = "c2b8e1a4d7f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "compare_cases",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("driver_id", sa.Uuid(), nullable=False),
        sa.Column("session_ids", sa.JSON(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["driver_id"], ["driver_profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_compare_cases_driver_id"),
        "compare_cases",
        ["driver_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_compare_cases_driver_id"), table_name="compare_cases")
    op.drop_table("compare_cases")
