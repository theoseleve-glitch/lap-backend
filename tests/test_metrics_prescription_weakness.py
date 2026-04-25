"""T177 — `prescriptions_created_total` (Counter) and
`weakness_episodes_open_total` (Gauge) surface at /metrics.

Counter increments on `PrescriptionRepo.create()`. Gauge tracks
the in-flight open episodes — `+1` on `WeaknessRepo.open()`, `-1`
on `WeaknessRepo.resolve()`. Tests assert the values change in
the right direction; no global-state assertions because metric
state leaks across the test session by design (Prometheus
singletons).
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient

from alembic import command
from app.main import app
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User
from infra.prescription_repo import PrescriptionRepo
from infra.weakness_repo import WeaknessRepo

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def _seed_driver(db: Any) -> DriverProfile:
    user = User(id=uuid6.uuid7(), clerk_user_id="u_t177", email="t177@t")
    driver = DriverProfile(
        id=uuid6.uuid7(), user_id=user.id, display_name="A"
    )
    db.add_all([user, driver])
    await db.commit()
    return driver


def _metric_value(name: str) -> float:
    """Scrape /metrics and return the bare-line value for `name`."""
    text = TestClient(app).get("/metrics").text
    for line in text.splitlines():
        if line.startswith(f"{name} "):
            return float(line.split()[-1])
    return 0.0


def test_prescriptions_created_total_counter_exists() -> None:
    text = TestClient(app).get("/metrics").text
    assert "prescriptions_created_total" in text


def test_weakness_episodes_open_total_gauge_exists() -> None:
    text = TestClient(app).get("/metrics").text
    assert "weakness_episodes_open_total" in text


async def test_create_prescription_increments_counter(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as db:
            driver = await _seed_driver(db)
            before = _metric_value("prescriptions_created_total")
            repo = PrescriptionRepo(db)
            await repo.create(
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                priority="high",
                title="x",
                rationale="r",
                expected_context={},
                success_condition={},
                status="open",
                drill_template_id="br.late_braking.v1",
            )
            after = _metric_value("prescriptions_created_total")
            assert after - before >= 1.0
    finally:
        await engine.dispose()


async def test_open_episode_increments_gauge(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as db:
            driver = await _seed_driver(db)
            before = _metric_value("weakness_episodes_open_total")
            repo = WeaknessRepo(db)
            await repo.open(
                driver_id=driver.id,
                category="braking",
                subcategory="late",
                severity="moderate",
                confidence=0.8,
                library_rule_id="br.x.v1",
                first_observed_at=datetime(2026, 4, 1, tzinfo=UTC),
                last_observed_at=datetime(2026, 4, 25, tzinfo=UTC),
                recurrence_count=2,
            )
            after = _metric_value("weakness_episodes_open_total")
            assert after - before == 1.0
    finally:
        await engine.dispose()


async def test_resolve_episode_decrements_gauge(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as db:
            driver = await _seed_driver(db)
            repo = WeaknessRepo(db)
            episode = await repo.open(
                driver_id=driver.id,
                category="braking",
                subcategory="late",
                severity="moderate",
                confidence=0.8,
                library_rule_id="br.x.v1",
                first_observed_at=datetime(2026, 4, 1, tzinfo=UTC),
                last_observed_at=datetime(2026, 4, 25, tzinfo=UTC),
                recurrence_count=2,
            )
            after_open = _metric_value("weakness_episodes_open_total")
            await repo.resolve(
                episode.id, resolved_at=datetime(2026, 4, 26, tzinfo=UTC)
            )
            after_resolve = _metric_value("weakness_episodes_open_total")
            assert after_resolve - after_open == -1.0
    finally:
        await engine.dispose()
