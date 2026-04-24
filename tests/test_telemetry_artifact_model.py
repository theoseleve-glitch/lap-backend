from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, Session, TelemetryArtifact, User

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


async def test_telemetry_artifact_roundtrips_fields(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            s = await _seed_session(db)
            artifact = TelemetryArtifact(
                id=uuid6.uuid7(),
                session_id=s.id,
                s3_key="sessions/2026/04/abc/samples.bin",
                sha256="a" * 64,
                size_bytes=5_368_709_120,  # 5 GiB — must fit in BigInteger
            )
            db.add(artifact)
            await db.commit()

            loaded = (await db.execute(select(TelemetryArtifact))).scalar_one()
            assert loaded.session_id == s.id
            assert loaded.s3_key == "sessions/2026/04/abc/samples.bin"
            assert loaded.sha256 == "a" * 64
            assert loaded.size_bytes == 5_368_709_120
    finally:
        await engine.dispose()


async def test_session_can_own_multiple_artifacts(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            s = await _seed_session(db)
            db.add_all(
                [
                    TelemetryArtifact(
                        id=uuid6.uuid7(),
                        session_id=s.id,
                        s3_key=f"sessions/x/{name}",
                        sha256="b" * 64,
                        size_bytes=1024,
                    )
                    for name in ("samples.bin", "events.parquet", "video.mp4")
                ]
            )
            await db.commit()
            rows = (await db.execute(select(TelemetryArtifact))).scalars().all()
            assert len(rows) == 3
    finally:
        await engine.dispose()


async def test_telemetry_artifact_s3_key_is_unique(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            s = await _seed_session(db)
            db.add(
                TelemetryArtifact(
                    id=uuid6.uuid7(),
                    session_id=s.id,
                    s3_key="sessions/x/samples.bin",
                    sha256="c" * 64,
                    size_bytes=10,
                )
            )
            await db.commit()
        async with session_factory() as db:
            db.add(
                TelemetryArtifact(
                    id=uuid6.uuid7(),
                    session_id=s.id,
                    s3_key="sessions/x/samples.bin",
                    sha256="d" * 64,
                    size_bytes=20,
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()
