"""T124 — Processing pipeline integration test (synthetic Sebring).

Exercises the full chain that runs inside `process_session_ingest`:
    blob_store fetch → bundle parse → timestamp align → lap detect
        → metrics + data-quality → Lap rows + SessionRecord row
        → status="ready"

The fixture is a small ndjson bundle (4 lap_number values, 5 samples
each = 20 samples). Three laps are emitted because lap 4 has no
successor. The bundle uses the irsdk_tick clock so timestamp
alignment is the identity case.

The "archived Sebring session" framing is aspirational — once real
captures land, the same shape pipeline will run on them. The
synthetic fixture proves the wiring today.
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


def _build_sebring_bundle() -> bytes:
    """4 lap_number transitions × 5 samples each, 90s lap times, all
    six baseline channels present so data_quality completeness is 1.0."""
    speeds_mps = [50.0, 60.0, 75.0, 65.0, 55.0]
    rows: list[dict[str, Any]] = []
    for lap_n in range(1, 5):
        for i, speed in enumerate(speeds_mps):
            t_ms = (lap_n - 1) * 90_000 + i * 18_000
            rows.append(
                {
                    "t_canonical_ns": t_ms * 1_000_000,
                    "t_source_ns": t_ms * 1_000_000,
                    "t_source_clock": "irsdk_tick",
                    "lap_number": lap_n,
                    "lap_dist_m": i * 1500.0,
                    "speed_mps": speed,
                    "throttle_pct": 0.7,
                    "brake_pct": 0.0,
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

    bundle_bytes = _build_sebring_bundle()
    chunk_filename = "chunk-0001.ndjson"
    s3_key = f"{session_id}/{chunk_filename}"
    blob_store.put(s3_key, bundle_bytes)

    async def _seed() -> None:
        async with factory() as db:
            db.add(User(id=user_id, clerk_user_id="u_t124", email="t124@t"))
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


def test_pipeline_marks_session_ready_after_processing(
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


def test_pipeline_persists_three_lap_rows_for_four_lap_stream(
    configured_pipeline: dict[str, Any],
) -> None:
    # 4 lap_number values in the stream → 3 completed laps emitted
    # (lap 4 has no successor). Lap rows are persisted in number order.
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
    assert all(lap.valid for lap in laps)


def test_pipeline_persists_session_record_with_metrics_payload(
    configured_pipeline: dict[str, Any],
) -> None:
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
    payload = rec.payload
    assert payload["metrics"]["best_lap_ms"] == 90_000
    assert payload["metrics"]["avg_lap_ms"] == 90_000
    # Three identical laps → stdev = 0 → consistency_ms = 0.
    assert payload["metrics"]["consistency_ms"] == 0
    assert payload["metrics"]["peak_speed_kph"] == 270.0


def test_pipeline_persists_data_quality_score_in_payload(
    configured_pipeline: dict[str, Any],
) -> None:
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
    payload = rec.payload
    quality = payload["data_quality"]
    # All baseline channels present, uniform 18s spacing >> any gap
    # threshold but evenly stable, so completeness=1, gaps>0, CV=0.
    assert quality["channel_completeness"] == 1.0
    assert quality["sample_rate_stability"] == 0.0
    # 18s intervals are gaps under the default 200ms threshold, but
    # they're uniform so rate stability is perfect.
    assert isinstance(quality["score"], int)
    assert 0 <= quality["score"] <= 100
