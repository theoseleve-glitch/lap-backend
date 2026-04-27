"""T175 add organizations.branding_logo_url

Revision ID: b8e2c5d4f9a1
Revises: a7d9f3e1c5b8
Create Date: 2026-04-27 12:30:00.000000

Adds a nullable `branding_logo_url` column to the
`organizations` table. NULL = no logo set; the client renders
a default placeholder. URL hosting (CDN/S3/data uri) is the
client's concern; this column is storage-only.

2048 chars matches the conservative HTTP URL ceiling and the
Pydantic max_length cap on the request shape.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b8e2c5d4f9a1"
down_revision: str | Sequence[str] | None = "a7d9f3e1c5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "organizations",
        sa.Column(
            "branding_logo_url",
            sa.String(length=2048),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("organizations", "branding_logo_url")
