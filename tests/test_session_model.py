from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, Session, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


def _make_driver(session: Any) -> tuple[Any, Any]:
    user = User(id=uuid6.uuid7(), clerk_user_id="u", email="u@test")
    driver = DriverProfile(id=uuid6.uuid7(), user_id=user.id, display_name="A")
    session.add(user)
    session.add(driver)
    return user, driver


async def test_session_row_roundtrips_manifest_json(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            _, driver = _make_driver(db)
            manifest = {
                "session_id": str(uuid6.uuid7()),
                "source": "iracing",
                "sample_rate_hz": 60.0,
                "telemetry_checksums": {"samples.bin": "sha256:abc"},
            }
            s = Session(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                manifest=manifest,
            )
            db.add(s)
            await db.commit()

            loaded = (await db.execute(select(Session))).scalar_one()
            assert loaded.manifest == manifest
            assert loaded.manifest["source"] == "iracing"
            assert loaded.status == "uploaded"
            assert isinstance(loaded.created_at, datetime)
    finally:
        await engine.dispose()


async def test_session_status_is_writable(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            _, driver = _make_driver(db)
            s = Session(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                manifest={},
                status="processing",
            )
            db.add(s)
            await db.commit()
            loaded = (await db.execute(select(Session))).scalar_one()
            assert loaded.status == "processing"
    finally:
        await engine.dispose()
