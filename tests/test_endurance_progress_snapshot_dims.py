"""T073 (sprint2-backend) — endurance-weighted DriverProgressSnapshot.

Adds three long-run dimensions to the existing 10-dim snapshot:
  - long_run_consistency (T070 stint stddev, aggregated)
  - pace_decay           (T071 stint slope, aggregated)
  - tire_preservation    (T072 stint composite, aggregated)

All three are nullable JSON columns (same JSON-with-JSONB-variant
pattern as the per-lap dims). Sprint-only drivers will have NULL
for these — additive non-breaking; existing snapshot rows in the
DB stay valid.

Two tests:
1. round-trip: insert a snapshot WITH all three endurance dims
   populated, reload, every dim survives.
2. nullability: a snapshot WITHOUT the new fields (the existing
   shape) still inserts and reloads cleanly with the dims as None.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, DriverProgressSnapshot, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _dim(score: float) -> dict[str, Any]:
    return {
        "score": score, "confidence": 0.8, "trend_14d": "flat",
        "last_evidence_session_id": None, "sample_count": 12,
    }


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


def _make_snapshot(driver_id: uuid6.UUID, **extra: Any) -> DriverProgressSnapshot:
    captured = datetime(2026, 4, 26, 12, 0, tzinfo=UTC)
    return DriverProgressSnapshot(
        id=uuid6.uuid7(),
        driver_id=driver_id,
        captured_at=captured,
        window="rolling_30d",
        window_start=captured,
        window_end=captured,
        braking=_dim(70.0),
        throttle_pickup=_dim(70.0),
        minimum_speed=_dim(70.0),
        line_consistency=_dim(70.0),
        tire_management=_dim(70.0),
        racecraft=_dim(70.0),
        fatigue_consistency=_dim(70.0),
        strategy_execution=_dim(70.0),
        setup_sensitivity=_dim(70.0),
        adaptation=_dim(70.0),
        overall_score=70.0, overall_confidence=0.8,
        generated_by="t073-test",
        **extra,
    )


# -- round-trip with endurance dims populated ---------------------


def test_endurance_dims_roundtrip_when_populated(
    upgraded_db_url: str,
) -> None:
    """Insert a snapshot with all three endurance dims populated,
    reload, every dim comes back unchanged. Pins the JSON column
    behavior — without it, a future migration that drops the JSON
    variant would silently coerce the dict."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                user = User(
                    id=uuid6.uuid7(), clerk_user_id="t073a", email="a@t",
                )
                driver = DriverProfile(
                    id=uuid6.uuid7(), user_id=user.id, display_name="A",
                )
                db.add_all([user, driver])
                await db.commit()
                snap = _make_snapshot(
                    driver.id,
                    long_run_consistency=_dim(72.0),
                    pace_decay=_dim(65.0),
                    tire_preservation=_dim(80.0),
                )
                db.add(snap)
                await db.commit()
            async with factory() as db:
                loaded = (
                    await db.execute(select(DriverProgressSnapshot))
                ).scalar_one()
                assert loaded.long_run_consistency == _dim(72.0)
                assert loaded.pace_decay == _dim(65.0)
                assert loaded.tire_preservation == _dim(80.0)
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- additive non-breaking shape ----------------------------------


def test_existing_snapshot_shape_without_endurance_dims_still_works(
    upgraded_db_url: str,
) -> None:
    """A snapshot constructed in the existing 10-dim style (no
    endurance fields) inserts and reloads with the three new dims
    coming back as None. Pin: existing producer code never has to
    change to keep working — sprint-only drivers stay sprint-only."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                user = User(
                    id=uuid6.uuid7(), clerk_user_id="t073b", email="b@t",
                )
                driver = DriverProfile(
                    id=uuid6.uuid7(), user_id=user.id, display_name="B",
                )
                db.add_all([user, driver])
                await db.commit()
                # No endurance dims in the constructor on purpose.
                snap = _make_snapshot(driver.id)
                db.add(snap)
                await db.commit()
            async with factory() as db:
                loaded = (
                    await db.execute(select(DriverProgressSnapshot))
                ).scalar_one()
                assert loaded.long_run_consistency is None
                assert loaded.pace_decay is None
                assert loaded.tire_preservation is None
        finally:
            await engine.dispose()

    asyncio.run(run())
