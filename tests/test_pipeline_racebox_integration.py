"""T125 — Processing pipeline integration test (synthetic RaceBox).

Same pipeline as T124 (Sebring/sim), but the fixture is shaped
like a real-life capture: racebox_gnss samples interleaved with
coremotion samples, per ADR-007. The GNSS frames carry
lap_number / lap_dist_m / speed_mps; the CoreMotion frames carry
IMU channels and no lap info — so lap detection is driven only by
the GNSS series, while CoreMotion samples ride along through
alignment + data-quality scoring.

This proves the pipeline doesn't have hidden assumptions about a
single clock source or the sim-biased default expected-channel
set: real-life data degrades the data_quality score (channels
not present on every frame) but still produces a valid payload.
"""

import asyncio
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from infra.celery_app import process_session_ingest
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, Lap, SessionRecord, TelemetryArtifact, User
from infra.orm import Session as SessionRow
from infra.storage import LocalBlobStore

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _build_racebox_bundle() -> bytes:
    """4 lap_number transitions × 3 GNSS frames + 3 CoreMotion frames per lap.

    Lap times: 90s. Peak speed: 80 m/s = 288 kph (lap 2 mid-lap GNSS).
    Mixed clocks force the pipeline to walk a heterogenous stream.
    """
    rows: list[dict[str, Any]] = []
    speeds_mps_per_lap = [
        [40.0, 60.0, 50.0],   # lap 1
        [55.0, 80.0, 65.0],   # lap 2 — global peak
        [45.0, 70.0, 55.0],   # lap 3
        [50.0, 60.0, 55.0],   # lap 4 (in-progress, no successor)
    ]
    for lap_n in range(1, 5):
        lap_start_ms = (lap_n - 1) * 90_000
        speeds = speeds_mps_per_lap[lap_n - 1]
        # 3 GNSS frames per lap at t=0/30s/60s within the lap.
        for i, speed in enumerate(speeds):
            t_ms = lap_start_ms + i * 30_000
            rows.append(
                {
                    "t_canonical_ns": t_ms * 1_000_000,
                    "t_source_ns": t_ms * 1_000_000,
                    "t_source_clock": "racebox_gnss",
                    "lap_number": lap_n,
                    "lap_dist_m": i * 2000.0,
                    "speed_mps": speed,
                    "lat": 27.453 + i * 0.0001,
                    "lon": -82.512 + i * 0.0001,
                }
            )
        # 3 CoreMotion frames per lap at offsets between GNSS frames.
        for i in range(3):
            t_ms = lap_start_ms + 15_000 + i * 30_000
            rows.append(
                {
                    "t_canonical_ns": t_ms * 1_000_000,
                    "t_source_ns": t_ms * 1_000_000,
                    "t_source_clock": "coremotion",
                    "accel_x_mps2": 0.5,
                    "accel_y_mps2": 0.2,
                    "accel_z_mps2": 9.81,
                    "gyro_z_dps": 5.0,
                }
            )
    return ("\n".join(json.dumps(r) for r in rows)).encode("utf-8")


@pytest.fixture
def configured_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[str, Any], None, None]:
    db_path = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    storage_root = tmp_path / "blobs"
    monkeypatch.setenv("STORAGE_ROOT", str(storage_root))
    monkeypatch.delenv("R2_BUCKET", raising=False)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    engine = make_engine(url)
    factory = make_session_factory(engine)
    blob_store = LocalBlobStore(storage_root)

    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_id = uuid6.uuid7()

    bundle_bytes = _build_racebox_bundle()
    s3_key = f"{session_id}/chunk-0001.ndjson"
    blob_store.put(s3_key, bundle_bytes)

    async def _seed() -> None:
        async with factory() as db:
            db.add(User(id=user_id, clerk_user_id="u_t125", email="t125@t"))
            db.add(
                DriverProfile(
                    id=driver_id, user_id=user_id, display_name="A"
                )
            )
            db.add(
                SessionRow(
                    id=session_id,
                    driver_id=driver_id,
                    manifest={
                        "bundle_version": "v2",
                        "track_layout": "sebring-international",
                        "real_hardware": "racebox",
                    },
                    status="uploaded",
                )
            )
            db.add(
                TelemetryArtifact(
                    session_id=session_id,
                    s3_key=s3_key,
                    sha256="0" * 64,
                    size_bytes=len(bundle_bytes),
                )
            )
            await db.commit()

    asyncio.run(_seed())
    yield {"factory": factory, "session_id": session_id}


def test_racebox_pipeline_marks_session_ready(
    configured_pipeline: dict[str, Any],
) -> None:
    factory = configured_pipeline["factory"]
    session_id = configured_pipeline["session_id"]

    process_session_ingest.run(str(session_id))

    async def _read() -> str:
        async with factory() as db:
            row = (
                await db.execute(
                    select(SessionRow).where(SessionRow.id == session_id)
                )
            ).scalar_one()
            return str(row.status)

    assert asyncio.run(_read()) == "ready"


def test_racebox_pipeline_detects_laps_only_from_gnss_frames(
    configured_pipeline: dict[str, Any],
) -> None:
    # CoreMotion frames have no lap_number, so they should never
    # appear as phantom lap transitions. 4 GNSS lap_number values
    # → 3 completed laps emitted.
    factory = configured_pipeline["factory"]
    session_id = configured_pipeline["session_id"]

    process_session_ingest.run(str(session_id))

    async def _read_laps() -> list[Lap]:
        async with factory() as db:
            return list(
                (
                    await db.execute(
                        select(Lap)
                        .where(Lap.session_id == session_id)
                        .order_by(Lap.lap_number)
                    )
                ).scalars()
            )

    laps = asyncio.run(_read_laps())
    assert [lap.lap_number for lap in laps] == [1, 2, 3]
    assert all(lap.time_ms == 90_000 for lap in laps)


def test_racebox_pipeline_peak_speed_comes_from_gnss(
    configured_pipeline: dict[str, Any],
) -> None:
    # CoreMotion frames carry no speed_mps. Peak comes from the
    # max GNSS speed: 80 m/s = 288 kph.
    factory = configured_pipeline["factory"]
    session_id = configured_pipeline["session_id"]

    process_session_ingest.run(str(session_id))

    async def _read_record() -> SessionRecord:
        async with factory() as db:
            return (
                await db.execute(
                    select(SessionRecord).where(
                        SessionRecord.session_id == session_id
                    )
                )
            ).scalar_one()

    rec = asyncio.run(_read_record())
    assert rec.payload["metrics"]["peak_speed_kph"] == 288.0


def test_racebox_pipeline_data_quality_reflects_partial_completeness(
    configured_pipeline: dict[str, Any],
) -> None:
    # Real-life capture: GNSS frames carry 3 of the 5 default
    # expected channels, CoreMotion frames carry 0 of 5. Half the
    # samples have a fraction of the channels → completeness is
    # strictly between 0 and 1, and the score is a valid int.
    factory = configured_pipeline["factory"]
    session_id = configured_pipeline["session_id"]

    process_session_ingest.run(str(session_id))

    async def _read_record() -> SessionRecord:
        async with factory() as db:
            return (
                await db.execute(
                    select(SessionRecord).where(
                        SessionRecord.session_id == session_id
                    )
                )
            ).scalar_one()

    rec = asyncio.run(_read_record())
    quality = rec.payload["data_quality"]
    assert 0.0 < quality["channel_completeness"] < 1.0
    assert isinstance(quality["score"], int)
    assert 0 <= quality["score"] <= 100
