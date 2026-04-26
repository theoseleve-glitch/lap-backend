"""T025 add transfer_links.divergence_points GIN index

Revision ID: b3d7a1e9c5f8
Revises: a1c5e8f2d4b6
Create Date: 2026-04-26 05:00:00.000000

GIN index with jsonb_path_ops over the divergence_points JSONB
column on transfer_links. Powers cheap containment queries like:

  SELECT * FROM transfer_links
  WHERE divergence_points @> '[{"track_distance_m": 312.5}]'

The use case: "which links exhibit a divergence at corner X?"
either for the rule engine (rule fires at distance D, find which
links exhibit it) or driver-facing analytics ("show me my
divergence-rich corners").

`jsonb_path_ops` is half the size of the default `jsonb_ops` and
supports `@>` only — and `@>` is the dominant query pattern. If
the future demands key-existence queries (`?`, `?|`) we'd flip to
`jsonb_ops`, but until then the smaller index wins.

Postgres-only: SQLite doesn't have GIN indexes, and our test
fixture runs on SQLite. The dialect guard makes this a no-op
under SQLite so the migration applies cleanly in tests.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "b3d7a1e9c5f8"
down_revision: str | Sequence[str] | None = "a1c5e8f2d4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX_NAME = "ix_transfer_links_divergence_points_gin"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.create_index(
        INDEX_NAME,
        "transfer_links",
        ["divergence_points"],
        postgresql_using="gin",
        postgresql_ops={"divergence_points": "jsonb_path_ops"},
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_index(INDEX_NAME, table_name="transfer_links")
