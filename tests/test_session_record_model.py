from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, Session, SessionRecord, User

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


async def test_session_record_roundtrips_payload(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            s = await _seed_session(db)
            payload = {
                "schema_version": "v1",
                "best_lap_ms": 84_321,
                "metrics": {"peak_speed_mps": 78.3},
            }
            record = SessionRecord(
                id=uuid6.uuid7(),
                session_id=s.id,
                payload=payload,
            )
            db.add(record)
            await db.commit()

            loaded = (await db.execute(select(SessionRecord))).scalar_one()
            assert loaded.payload == payload
            assert loaded.payload["metrics"]["peak_speed_mps"] == 78.3
    finally:
        await engine.dispose()


async def test_session_record_session_id_is_unique(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            s = await _seed_session(db)
            db.add(SessionRecord(id=uuid6.uuid7(), session_id=s.id, payload={}))
            await db.commit()
        async with session_factory() as db:
            db.add(SessionRecord(id=uuid6.uuid7(), session_id=s.id, payload={}))
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()
