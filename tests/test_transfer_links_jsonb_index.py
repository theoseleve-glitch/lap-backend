"""T025 (sprint2-backend) — bridge storage: divergence_points
JSONB indexed by track_distance_m.

Each TransferLink stores its `divergence_points` as a JSONB array
on Postgres. To make queries like "which links exhibit a divergence
at corner X (track_distance_m ≈ D)?" cheap, we add a GIN index
with the `jsonb_path_ops` operator class.

This test pins the *schema* contract: the index exists in the ORM
metadata with the right operator class + access method. The actual
SQL emission is exercised by the Alembic migration tests.

`jsonb_path_ops` is intentional (vs. the default `jsonb_ops`):
smaller, faster, supports `@>` only — and `@>` is the dominant
query pattern (e.g.
`divergence_points @> '[{"track_distance_m": 312.5}]'`).
SQLite (our test fixture DB) doesn't support GIN, so the migration
guards on dialect — but the ORM metadata declaration is the same
for both dialects, which is what this test inspects.
"""

from __future__ import annotations

from sqlalchemy import Index

from infra.orm import TransferLink

EXPECTED_INDEX_NAME = "ix_transfer_links_divergence_points_gin"


def test_transfer_links_has_gin_index_on_divergence_points() -> None:
    table = TransferLink.__table__
    idx = next(
        (i for i in table.indexes if i.name == EXPECTED_INDEX_NAME),
        None,
    )
    assert idx is not None, (
        f"expected index {EXPECTED_INDEX_NAME!r} on transfer_links — "
        f"got: {[i.name for i in table.indexes]}"
    )
    assert isinstance(idx, Index)


def test_transfer_links_index_targets_divergence_points_column() -> None:
    table = TransferLink.__table__
    idx = next(
        i for i in table.indexes if i.name == EXPECTED_INDEX_NAME
    )
    column_names = [c.name for c in idx.columns]
    assert column_names == ["divergence_points"], (
        f"index columns: {column_names}"
    )


def test_transfer_links_index_uses_gin_with_jsonb_path_ops() -> None:
    """Pin the operator class + access method — these decisions are
    deliberate (jsonb_path_ops is half the size of jsonb_ops and is
    the right choice for our containment-only query pattern). If a
    future change drops to plain BTREE or removes the path-ops
    qualifier, this trips."""
    table = TransferLink.__table__
    idx = next(
        i for i in table.indexes if i.name == EXPECTED_INDEX_NAME
    )
    assert idx.dialect_kwargs.get("postgresql_using") == "gin"
    ops = idx.dialect_kwargs.get("postgresql_ops") or {}
    assert ops.get("divergence_points") == "jsonb_path_ops", (
        f"postgresql_ops: {ops}"
    )
