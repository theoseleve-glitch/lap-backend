"""T191 — performance indexes migration test.

Pins that the T191 migration:
  - applies cleanly to a fresh database
  - creates exactly the 6 composite indexes documented in the
    migration's docstring
  - the index order (column tuple) is what we expect — order
    matters for B-tree prefix matching

A future PR that drops one of these indexes for "cleanup"
breaks this test, forcing a deliberate decision rather than a
silent regression.
"""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect

from alembic import command

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


_EXPECTED_INDEXES: dict[str, tuple[str, list[str]]] = {
    "ix_weakness_episodes_driver_id_status": (
        "weakness_episodes",
        ["driver_id", "status"],
    ),
    "ix_action_prescriptions_driver_id_status": (
        "action_prescriptions",
        ["driver_id", "status"],
    ),
    "ix_progress_snapshots_driver_id_captured_at": (
        "driver_progress_snapshots",
        ["driver_id", "captured_at"],
    ),
    "ix_sessions_driver_id_created_at": (
        "sessions",
        ["driver_id", "created_at"],
    ),
    "ix_organization_members_org_id_role": (
        "organization_members",
        ["org_id", "role"],
    ),
    "ix_organization_invitations_org_accepted_expires": (
        "organization_invitations",
        ["org_id", "accepted_at", "expires_at"],
    ),
}


def test_t191_migration_creates_all_expected_indexes(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "perf.sqlite"
    sync_url = f"sqlite:///{db_path.as_posix()}"
    import os

    os.environ["DATABASE_URL"] = (
        f"sqlite+aiosqlite:///{db_path.as_posix()}"
    )

    # Run alembic upgrade head — this applies T191 alongside all
    # prior migrations. If the migration has a syntax error or a
    # bad SQL statement, this raises.
    cfg = AlembicConfig(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")

    # Inspect the actual schema — the migration's intent must
    # match what landed on disk.
    engine = create_engine(sync_url)
    inspector = inspect(engine)
    for index_name, (
        table,
        expected_cols,
    ) in _EXPECTED_INDEXES.items():
        idxs = inspector.get_indexes(table)
        match = next(
            (idx for idx in idxs if idx["name"] == index_name),
            None,
        )
        assert match is not None, (
            f"missing index {index_name!r} on {table!r} — "
            f"have: {[i['name'] for i in idxs]}"
        )
        assert match["column_names"] == expected_cols, (
            f"index {index_name!r} columns {match['column_names']} "
            f"!= expected {expected_cols}"
        )

    engine.dispose()


def test_t191_migration_is_reversible(
    tmp_path: Path,
) -> None:
    """Apply head, downgrade T191, verify the indexes vanish.
    Catches a downgrade() that's missing a drop_index call."""
    db_path = tmp_path / "perf_rev.sqlite"
    sync_url = f"sqlite:///{db_path.as_posix()}"
    import os

    os.environ["DATABASE_URL"] = (
        f"sqlite+aiosqlite:///{db_path.as_posix()}"
    )

    cfg = AlembicConfig(str(ALEMBIC_INI))
    command.upgrade(cfg, "head")
    # Step back one revision (T191's predecessor is b8e2c5d4f9a1).
    command.downgrade(cfg, "b8e2c5d4f9a1")

    engine = create_engine(sync_url)
    inspector = inspect(engine)
    for index_name, (table, _cols) in _EXPECTED_INDEXES.items():
        idx_names = {idx["name"] for idx in inspector.get_indexes(table)}
        assert index_name not in idx_names, (
            f"downgrade left behind {index_name!r} on {table!r}"
        )
    engine.dispose()
