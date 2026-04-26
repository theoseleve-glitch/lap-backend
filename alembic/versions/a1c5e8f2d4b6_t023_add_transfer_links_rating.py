"""T023 add transfer_links.usefulness_rating + rated_at

Revision ID: a1c5e8f2d4b6
Revises: f6b9c8d3e1a2
Create Date: 2026-04-26 04:00:00.000000

Two new nullable columns on transfer_links so a driver can rate a
bridge link's usefulness (thumbs up/down) and the rule-health audit
can answer "is this rule still producing useful suggestions?".

`usefulness_rating` is short text — the schema enum lives in
lap_schemas (`BridgeLinkRating = Literal["up", "down"]`); we don't
need a DB-level CHECK because all writes go through the validated
POST endpoint.

`rated_at` is server-stamped on POST; nullable so unrated rows
remain unrated.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1c5e8f2d4b6"
down_revision: str | Sequence[str] | None = "f6b9c8d3e1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "transfer_links",
        sa.Column("usefulness_rating", sa.String(length=8), nullable=True),
    )
    op.add_column(
        "transfer_links",
        sa.Column("rated_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transfer_links", "rated_at")
    op.drop_column("transfer_links", "usefulness_rating")
