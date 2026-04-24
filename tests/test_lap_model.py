from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, Lap, Session, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def _seed_session(db) -> Session:  # type: ignore[no-untyped-def]
    user = User(id=uuid6.uuid7(), clerk_user_id="u", email="u@test")
    driver = DriverProfile(id=uuid6.uuid7(), user_id=user.id, display_name="A")
    session = Session(id=uuid6.uuid7(), driver_id=driver.id, manifest={})
    db.add_all([user, driver, session])
    await db.commit()
    return session


async def test_lap_roundtrips_fields(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            s = await _seed_session(db)
            lap = Lap(
                id=uuid6.uuid7(),
                session_id=s.id,
                lap_number=3,
                time_ms=84_321,
                sector_times=[28_100, 27_900, 28_321],
                valid=True,
            )
            db.add(lap)
            await db.commit()

            loaded = (await db.execute(select(Lap))).scalar_one()
            assert loaded.session_id == s.id
            assert loaded.lap_number == 3
            assert loaded.time_ms == 84_321
            assert loaded.sector_times == [28_100, 27_900, 28_321]
            assert loaded.valid is True
    finally:
        await engine.dispose()


async def test_session_can_own_multiple_laps(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            s = await _seed_session(db)
            db.add_all(
                [
                    Lap(
                        id=uuid6.uuid7(),
                        session_id=s.id,
                        lap_number=n,
                        time_ms=80_000 + n,
                        sector_times=[],
                        valid=True,
                    )
                    for n in (1, 2, 3, 4, 5)
                ]
            )
            await db.commit()
            rows = (await db.execute(select(Lap))).scalars().all()
            assert len(rows) == 5
    finally:
        await engine.dispose()


async def test_lap_session_id_and_number_are_unique_together(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            s = await _seed_session(db)
            db.add(
                Lap(
                    id=uuid6.uuid7(),
                    session_id=s.id,
                    lap_number=7,
                    time_ms=84_000,
                    sector_times=[],
                    valid=True,
                )
            )
            await db.commit()
        async with session_factory() as db:
            db.add(
                Lap(
                    id=uuid6.uuid7(),
                    session_id=s.id,
                    lap_number=7,
                    time_ms=85_000,
                    sector_times=[],
                    valid=False,
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()


async def test_invalid_lap_is_writable(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            s = await _seed_session(db)
            db.add(
                Lap(
                    id=uuid6.uuid7(),
                    session_id=s.id,
                    lap_number=1,
                    time_ms=90_000,
                    sector_times=[30_000, 30_000, 30_000],
                    valid=False,
                )
            )
            await db.commit()
            loaded = (await db.execute(select(Lap))).scalar_one()
            assert loaded.valid is False
    finally:
        await engine.dispose()
