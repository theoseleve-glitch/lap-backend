from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, TransferLink, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def _seed_driver(db) -> DriverProfile:  # type: ignore[no-untyped-def]
    user = User(id=uuid6.uuid7(), clerk_user_id="u", email="u@test")
    driver = DriverProfile(id=uuid6.uuid7(), user_id=user.id, display_name="A")
    db.add_all([user, driver])
    await db.commit()
    return driver


async def test_transfer_link_roundtrips_all_fields(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            sim_sid = str(uuid6.uuid7())
            real_sid = str(uuid6.uuid7())
            created = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)
            report_id = uuid6.uuid7()
            divergences = [
                {
                    "track_distance_m": 420.5,
                    "metric": "brake_pressure",
                    "sim_value": 82.0,
                    "real_value": 95.5,
                    "divergence_type": "intensity",
                },
                {
                    "track_distance_m": 1200.0,
                    "metric": "line_y",
                    "sim_value": 1.2,
                    "real_value": 2.8,
                    "divergence_type": "shape",
                },
            ]
            link = TransferLink(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                created_at=created,
                created_by="auto",
                session_pair=[sim_sid, real_sid],
                link_reason="same_track",
                shape_match_score=0.78,
                intensity_match_score=0.62,
                divergence_points=divergences,
                transferred_skills=["braking", "line_consistency"],
                non_transferred_skills=["tire_management"],
                generated_report_id=report_id,
            )
            db.add(link)
            await db.commit()

            loaded = (await db.execute(select(TransferLink))).scalar_one()
            assert loaded.driver_id == driver.id
            assert loaded.created_at == created
            assert loaded.created_by == "auto"
            assert loaded.session_pair == [sim_sid, real_sid]
            assert loaded.link_reason == "same_track"
            assert loaded.shape_match_score == 0.78
            assert loaded.intensity_match_score == 0.62
            assert loaded.divergence_points == divergences
            assert loaded.transferred_skills == ["braking", "line_consistency"]
            assert loaded.non_transferred_skills == ["tire_management"]
            assert loaded.generated_report_id == report_id
    finally:
        await engine.dispose()


async def test_transfer_link_defaults_empty_lists_and_nullable_report(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            link = TransferLink(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                created_by="user",
                session_pair=[str(uuid6.uuid7()), str(uuid6.uuid7())],
                link_reason="manual",
                shape_match_score=0.5,
                intensity_match_score=0.5,
            )
            db.add(link)
            await db.commit()
            loaded = (await db.execute(select(TransferLink))).scalar_one()
            assert loaded.divergence_points == []
            assert loaded.transferred_skills == []
            assert loaded.non_transferred_skills == []
            assert loaded.generated_report_id is None
    finally:
        await engine.dispose()
