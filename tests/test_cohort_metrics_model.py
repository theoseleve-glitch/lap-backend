"""T054 (sprint2-backend) — anonymized cohort_metrics table.

Privacy-by-design metric pool: every row is just `(car_id, track_id,
metric, value, session_time)` with NO driver_id. This is the
chokepoint that makes cohort percentile compute (T056+) safe to run
without surfacing per-driver data — there is literally nowhere in
the row to put a driver back.

Two tests:
1. round-trip: insert → reload → fields match. Pins the column
   schema and types via real DB I/O.
2. privacy: inspect the live `cohort_metrics` table and assert
   `driver_id` is NOT among its columns. Catches both ORM-level
   and migration-level regressions; the value of this test is
   that future contributors who try to add a driver_id "for
   debugging" trip on it.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncEngine

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import CohortMetric

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


# -- round-trip ---------------------------------------------------


def test_cohort_metric_roundtrips_all_fields(upgraded_db_url: str) -> None:
    """Insert one row, reload it, every field comes back unchanged.
    Pins that the schema columns exist with the right types."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                car_id = uuid6.uuid7()
                track_id = uuid6.uuid7()
                session_time = datetime(2026, 4, 26, 14, 30, tzinfo=UTC)
                row = CohortMetric(
                    id=uuid6.uuid7(),
                    car_id=car_id,
                    track_id=track_id,
                    metric="braking",
                    value=0.732,
                    session_time=session_time,
                )
                db.add(row)
                await db.commit()
                loaded = (
                    await db.execute(select(CohortMetric))
                ).scalar_one()
                assert loaded.car_id == car_id
                assert loaded.track_id == track_id
                assert loaded.metric == "braking"
                assert loaded.value == 0.732
                assert loaded.session_time == session_time
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- privacy invariant --------------------------------------------


def test_cohort_metrics_table_has_no_driver_id_column(
    upgraded_db_url: str,
) -> None:
    """T054 acceptance: cohort_metrics has NO driver_id column.
    Anonymization is enforced at the schema level — there is no
    field to write a driver_id into. This test guards against
    a future "add driver_id for debugging" regression that would
    silently de-anonymize the cohort pool.
    """

    async def run() -> None:
        engine: AsyncEngine = make_engine(upgraded_db_url)
        try:
            async with engine.begin() as conn:
                column_names = await conn.run_sync(
                    lambda sync_conn: {
                        c["name"] for c in inspect(sync_conn).get_columns(
                            "cohort_metrics"
                        )
                    }
                )
            assert "driver_id" not in column_names, (
                f"cohort_metrics must NOT have driver_id column "
                f"(privacy invariant); columns are: {sorted(column_names)}"
            )
            # Sanity: the contract columns ARE present.
            assert {
                "id", "car_id", "track_id", "metric", "value",
                "session_time",
            }.issubset(column_names)
        finally:
            await engine.dispose()

    asyncio.run(run())
