"""T055 (sprint2-backend) — cohort ingestion job.

When a session finishes processing AND the driver is opted in,
append one row per skill dimension to `cohort_metrics`. The
privacy gate is INSIDE the ingestion function (not a caller's
responsibility) so the path-of-least-resistance always opts a
non-consenting driver out — there's no caller-side check that
can be forgotten.

Acceptance:
  - opted-in driver: 10 dimensions × 1 session = 10 rows
  - opted-out driver: 0 rows (silent skip, not error)
  - rows use MASTER track/car IDs (cross-provider equivalence),
    not the provider-specific catalog row IDs
  - session_time = session.created_at
  - metric values come from the snapshot's per-dimension `score`
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
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CarModel,
    CohortMetric,
    DriverProfile,
    DriverProgressSnapshot,
    TrackLayout,
    User,
)
from infra.orm import (
    Session as SessionRow,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


_DIMENSIONS = (
    "braking", "throttle_pickup", "minimum_speed", "line_consistency",
    "tire_management", "racecraft", "fatigue_consistency",
    "strategy_execution", "setup_sensitivity", "adaptation",
)


def _dim(score: float) -> dict[str, Any]:
    return {
        "score": score, "confidence": 0.8, "trend_14d": "flat",
        "last_evidence_session_id": None, "sample_count": 5,
    }


async def _seed(
    factory: Any, *, opt_in: bool, score_for: dict[str, float] | None = None
) -> dict[str, Any]:
    """Seed a complete fixture: user, opt-in driver, track, car,
    session, and a DriverProgressSnapshot covering the session."""
    score_for = score_for or {}
    async with factory() as db:
        user = User(id=uuid6.uuid7(), clerk_user_id="alice", email="a@t")
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name="Alice",
            cohort_opt_in=opt_in,
        )
        master_track = uuid6.uuid7()
        master_car = uuid6.uuid7()
        track = TrackLayout(
            id=uuid6.uuid7(), source_provider="iracing",
            name="Spa", layout_name="GP",
            start_finish_lat=50.4, start_finish_lon=5.97,
            start_finish_heading_deg=10.0,
            sector_checkpoints=[], corner_names=[],
            master_track_id=master_track,
        )
        car = CarModel(
            id=uuid6.uuid7(), source_provider="iracing", name="911",
            car_class="gt3", master_car_id=master_car,
        )
        session_time = datetime(2026, 4, 26, 14, 0, tzinfo=UTC)
        session = SessionRow(
            id=uuid6.uuid7(), driver_id=driver.id,
            manifest={"track_id": str(track.id), "car_id": str(car.id)},
            created_at=session_time, status="processed",
        )
        snapshot = DriverProgressSnapshot(
            id=uuid6.uuid7(), driver_id=driver.id,
            captured_at=session_time, window="session",
            window_start=session_time, window_end=session_time,
            braking=_dim(score_for.get("braking", 70.0)),
            throttle_pickup=_dim(score_for.get("throttle_pickup", 65.0)),
            minimum_speed=_dim(score_for.get("minimum_speed", 60.0)),
            line_consistency=_dim(score_for.get("line_consistency", 75.0)),
            tire_management=_dim(score_for.get("tire_management", 55.0)),
            racecraft=_dim(score_for.get("racecraft", 50.0)),
            fatigue_consistency=_dim(
                score_for.get("fatigue_consistency", 80.0)
            ),
            strategy_execution=_dim(
                score_for.get("strategy_execution", 45.0)
            ),
            setup_sensitivity=_dim(
                score_for.get("setup_sensitivity", 40.0)
            ),
            adaptation=_dim(score_for.get("adaptation", 35.0)),
            overall_score=58.0, overall_confidence=0.7,
            generated_by="t055-test",
        )
        db.add_all([user, driver, track, car, session, snapshot])
        await db.commit()
        return {
            "session_id": session.id,
            "snapshot_id": snapshot.id,
            "master_track": master_track,
            "master_car": master_car,
            "session_time": session_time,
        }


def test_opt_in_driver_writes_one_row_per_dimension(
    upgraded_db_url: str,
) -> None:
    """Opt-in: 10 dimensions in the snapshot → 10 cohort_metric rows.
    The privacy invariant is that we don't drop opt-in data; the
    inverse-privacy invariant is that we don't smuggle anything
    EXTRA either (no row count >10)."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed(factory, opt_in=True)
            async with factory() as db:
                session = (
                    await db.execute(
                        select(SessionRow).where(
                            SessionRow.id == seed["session_id"]
                        )
                    )
                ).scalar_one()
                snapshot = (
                    await db.execute(
                        select(DriverProgressSnapshot).where(
                            DriverProgressSnapshot.id == seed["snapshot_id"]
                        )
                    )
                ).scalar_one()
                n = await ingest_cohort_metrics(
                    db, session=session, progress_snapshot=snapshot
                )
                await db.commit()
            assert n == 10
            async with factory() as db:
                rows = (
                    (await db.execute(select(CohortMetric)))
                    .scalars().all()
                )
                assert len(rows) == 10
                assert {r.metric for r in rows} == set(_DIMENSIONS)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_opt_out_driver_writes_zero_rows(upgraded_db_url: str) -> None:
    """Privacy gate: an opted-out driver's session is silently
    skipped (returns 0). Critically NOT an error — the ingestion
    job runs over many sessions and a single non-consenting driver
    must not break the batch."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed(factory, opt_in=False)
            async with factory() as db:
                session = (
                    await db.execute(
                        select(SessionRow).where(
                            SessionRow.id == seed["session_id"]
                        )
                    )
                ).scalar_one()
                snapshot = (
                    await db.execute(
                        select(DriverProgressSnapshot).where(
                            DriverProgressSnapshot.id == seed["snapshot_id"]
                        )
                    )
                ).scalar_one()
                n = await ingest_cohort_metrics(
                    db, session=session, progress_snapshot=snapshot
                )
                await db.commit()
            assert n == 0
            async with factory() as db:
                rows = (
                    (await db.execute(select(CohortMetric)))
                    .scalars().all()
                )
                assert rows == []
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_rows_use_master_ids_not_provider_ids(
    upgraded_db_url: str,
) -> None:
    """Cross-provider equivalence: cohort_metrics.car_id stores
    master_car_id (the equivalence-class ID) NOT car_models.id
    (the per-provider catalog ID). Same for track. This is what
    lets the percentile compute pool e.g. iRacing-Spa with
    AssettoCorsa-Spa as the same cohort."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed(factory, opt_in=True)
            async with factory() as db:
                session = (
                    await db.execute(
                        select(SessionRow).where(
                            SessionRow.id == seed["session_id"]
                        )
                    )
                ).scalar_one()
                snapshot = (
                    await db.execute(
                        select(DriverProgressSnapshot).where(
                            DriverProgressSnapshot.id == seed["snapshot_id"]
                        )
                    )
                ).scalar_one()
                await ingest_cohort_metrics(
                    db, session=session, progress_snapshot=snapshot
                )
                await db.commit()
            async with factory() as db:
                rows = (
                    (await db.execute(select(CohortMetric)))
                    .scalars().all()
                )
                assert all(r.car_id == seed["master_car"] for r in rows)
                assert all(r.track_id == seed["master_track"] for r in rows)
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_row_value_equals_dimension_score(upgraded_db_url: str) -> None:
    """Pin the value extraction: the metric value stored is the
    dimension's `score` field (not confidence, not sample_count).
    Mutation guard for "value = dim.confidence" or similar bug."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed(
                factory, opt_in=True,
                score_for={"braking": 73.5, "racecraft": 41.2},
            )
            async with factory() as db:
                session = (
                    await db.execute(
                        select(SessionRow).where(
                            SessionRow.id == seed["session_id"]
                        )
                    )
                ).scalar_one()
                snapshot = (
                    await db.execute(
                        select(DriverProgressSnapshot).where(
                            DriverProgressSnapshot.id == seed["snapshot_id"]
                        )
                    )
                ).scalar_one()
                await ingest_cohort_metrics(
                    db, session=session, progress_snapshot=snapshot
                )
                await db.commit()
            async with factory() as db:
                rows = (
                    (await db.execute(select(CohortMetric)))
                    .scalars().all()
                )
                by_metric = {r.metric: r.value for r in rows}
                assert by_metric["braking"] == 73.5
                assert by_metric["racecraft"] == 41.2
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_session_time_matches_session_created_at(
    upgraded_db_url: str,
) -> None:
    """session_time on each cohort_metric row mirrors the source
    session's created_at — the percentile compute uses this to
    filter recent rows for the rebuild window. We compare against
    the post-reload created_at (not the passed-in datetime) since
    SQLite drops timezone on round-trip; the contract pinned here
    is 'cohort_metric.session_time == session.created_at as
    persisted', which Postgres preserves with TZ intact."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            seed = await _seed(factory, opt_in=True)
            async with factory() as db:
                session = (
                    await db.execute(
                        select(SessionRow).where(
                            SessionRow.id == seed["session_id"]
                        )
                    )
                ).scalar_one()
                snapshot = (
                    await db.execute(
                        select(DriverProgressSnapshot).where(
                            DriverProgressSnapshot.id == seed["snapshot_id"]
                        )
                    )
                ).scalar_one()
                expected_time = session.created_at
                await ingest_cohort_metrics(
                    db, session=session, progress_snapshot=snapshot
                )
                await db.commit()
            async with factory() as db:
                rows = (
                    (await db.execute(select(CohortMetric)))
                    .scalars().all()
                )
                assert all(r.session_time == expected_time for r in rows)
        finally:
            await engine.dispose()

    asyncio.run(run())
