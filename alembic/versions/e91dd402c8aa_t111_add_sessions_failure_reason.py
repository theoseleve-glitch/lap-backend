"""T111 add sessions.failure_reason

Revision ID: e91dd402c8aa
Revises: d8837309e328
Create Date: 2026-04-25 09:55:00.000000

"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "e91dd402c8aa"
down_revision: str | Sequence[str] | None = "d8837309e328"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "sessions",
        sa.Column("failure_reason", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("sessions", "failure_reason")
