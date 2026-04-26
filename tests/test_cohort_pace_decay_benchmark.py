"""T078 (sprint2-backend) — pace-decay cohort percentile available.

Long-run benchmark: pace_decay (T071 OLS slope) percentilable
against the (master_car, master_track) cohort the same way the
ten per-lap dims are. Requires:
  - T055 ingest writes a cohort_metrics row for pace_decay when
    the opt-in driver's snapshot has the dim populated
  - T057 cohort_percentile_for_value returns a non-None rank for
    metric='pace_decay' once the cohort clears the k=20 threshold

Sprint-only drivers (snapshot.pace_decay == None) skip silently
— additive non-breaking; the existing 10-dim ingest test is
unaffected.
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
from domain.cohort_ingest import ingest_cohort_metrics
from domain.percentile_compute import cohort_percentile_for_value
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CarModel,
    CohortMetric,
    DriverProfile,
    DriverProgressSnapshot,
    TrackLayout,
    User,
)
from infra.orm import Session as SessionRow

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


def _dim(score: float) -> dict[str, Any]:
    return {
        "score": score, "confidence": 0.8, "trend_14d": "flat",
        "last_evidence_session_id": None, "sample_count": 5,
    }


async def _seed_one(
    factory: Any,
    *,
    label: str,
    pace_decay_score: float | None,
    master_track: uuid6.UUID,
    master_car: uuid6.UUID,
) -> uuid6.UUID:
    """Seed a single opt-in driver + session + snapshot.

    pace_decay_score=None → snapshot.pace_decay is None (the
    sprint-only state); ingestion must skip silently."""
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(), clerk_user_id=label, email=f"{label}@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name=label,
            cohort_opt_in=True,
        )
        track = TrackLayout(
            id=uuid6.uuid7(), source_provider=f"iracing-{label}",
            name=f"Spa-{label}", layout_name="GP",
            start_finish_lat=50.4, start_finish_lon=5.97,
            start_finish_heading_deg=10.0,
            sector_checkpoints=[], corner_names=[],
            master_track_id=master_track,
        )
        car = CarModel(
            id=uuid6.uuid7(), source_provider=f"iracing-{label}",
            name=f"911-{label}", car_class="gt3",
            master_car_id=master_car,
        )
        session_time = datetime(2026, 4, 26, tzinfo=UTC)
        session = SessionRow(
            id=uuid6.uuid7(), driver_id=driver.id,
            manifest={
                "track_id": str(track.id), "car_id": str(car.id),
            },
            created_at=session_time, status="processed",
        )
        snapshot = DriverProgressSnapshot(
            id=uuid6.uuid7(), driver_id=driver.id,
            captured_at=session_time, window="session",
            window_start=session_time, window_end=session_time,
            braking=_dim(60.0), throttle_pickup=_dim(60.0),
            minimum_speed=_dim(60.0), line_consistency=_dim(60.0),
            tire_management=_dim(60.0), racecraft=_dim(60.0),
            fatigue_consistency=_dim(60.0),
            strategy_execution=_dim(60.0),
            setup_sensitivity=_dim(60.0), adaptation=_dim(60.0),
            pace_decay=(
                _dim(pace_decay_score)
                if pace_decay_score is not None else None
            ),
            overall_score=60.0, overall_confidence=0.7,
            generated_by="t078-test",
        )
        db.add_all([user, driver, track, car, session, snapshot])
        await db.commit()
        await ingest_cohort_metrics(
            db, session=session, progress_snapshot=snapshot,
        )
        await db.commit()
        return driver.id


# -- ingestion writes pace_decay rows ------------------------------


def test_opt_in_driver_with_pace_decay_writes_cohort_metric(
    upgraded_db_url: str,
) -> None:
    """When the snapshot has pace_decay populated, ingestion
    appends a cohort_metric row with metric='pace_decay' and the
    score as the value. Pin: pace_decay must join the cohort
    pool, otherwise T078's percentile path has no data to rank."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            mt = uuid6.uuid7()
            mc = uuid6.uuid7()
            await _seed_one(
                factory, label="alice", pace_decay_score=0.12,
                master_track=mt, master_car=mc,
            )
            async with factory() as db:
                rows = (
                    await db.execute(
                        select(CohortMetric).where(
                            CohortMetric.metric == "pace_decay",
                        )
                    )
                ).scalars().all()
                assert len(rows) == 1
                assert rows[0].car_id == mc
                assert rows[0].track_id == mt
                assert rows[0].value == 0.12
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- sprint-only driver still works (additive non-breaking) -------


def test_snapshot_without_pace_decay_writes_no_pace_decay_row(
    upgraded_db_url: str,
) -> None:
    """A sprint-only driver (snapshot.pace_decay is None) must
    not produce a pace_decay row. Pin: the existing 10-dim
    ingest behavior survives — adding pace_decay to the loop
    didn't make it required."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            mt = uuid6.uuid7()
            mc = uuid6.uuid7()
            await _seed_one(
                factory, label="bob", pace_decay_score=None,
                master_track=mt, master_car=mc,
            )
            async with factory() as db:
                rows = (
                    await db.execute(
                        select(CohortMetric).where(
                            CohortMetric.metric == "pace_decay",
                        )
                    )
                ).scalars().all()
                assert rows == []
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- percentile available when cohort unlocks ---------------------


def test_pace_decay_percentile_available_with_unlocked_cohort(
    upgraded_db_url: str,
) -> None:
    """20 opt-in drivers (cohort threshold k=20) all contribute
    pace_decay rows. cohort_percentile_for_value must return a
    rank in [0, 1] for metric='pace_decay'. Pin: T071's per-stint
    score plugs into T057's existing rank machinery without any
    metric-name-specific code path."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            mt = uuid6.uuid7()
            mc = uuid6.uuid7()
            for i in range(20):
                await _seed_one(
                    factory, label=f"d{i}",
                    pace_decay_score=0.05 + i * 0.01,
                    master_track=mt, master_car=mc,
                )
            async with factory() as db:
                rank = await cohort_percentile_for_value(
                    db, car_id=mc, track_id=mt,
                    metric="pace_decay", value=0.10,
                )
                assert rank is not None
                assert 0.0 <= rank <= 1.0
        finally:
            await engine.dispose()

    asyncio.run(run())
