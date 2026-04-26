"""T029 add transfer_links.stale_at

Revision ID: c4f8e2b1d6a9
Revises: b3d7a1e9c5f8
Create Date: 2026-04-26 06:00:00.000000

Lifecycle column for bridge links: when a session referenced in a
link's session_pair is deleted, the backend stamps stale_at.

Nullable — most links live their whole life with stale_at=NULL.
No DB-level constraint enforcing the relationship; session_pair is
JSONB and the helper that maintains stale_at lives in the domain
layer (domain.bridge.mark_transfer_links_stale_for_session).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c4f8e2b1d6a9"
down_revision: str | Sequence[str] | None = "b3d7a1e9c5f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transfer_links",
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transfer_links", "stale_at")
