from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import (
    DriverProfile,
    Session,
    UploadQueueItem,
    User,
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


async def _seed_session(db) -> Session:  # type: ignore[no-untyped-def]
    user = User(id=uuid6.uuid7(), clerk_user_id="u", email="u@test")
    driver = DriverProfile(id=uuid6.uuid7(), user_id=user.id, display_name="A")
    sess = Session(id=uuid6.uuid7(), driver_id=driver.id, manifest={})
    db.add_all([user, driver, sess])
    await db.commit()
    return sess


async def test_upload_queue_item_roundtrips_all_fields(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            sess = await _seed_session(db)
            retry = datetime(2026, 4, 25, 14, 30, tzinfo=UTC)
            item = UploadQueueItem(
                id=uuid6.uuid7(),
                session_id=sess.id,
                state="failed",
                attempt_count=3,
                next_retry_at=retry,
                last_http_code=503,
                last_error="Service Unavailable",
                chunks_sent=4,
                chunks_total=8,
                idempotency_key="ulid_abc123_session_xyz",
            )
            db.add(item)
            await db.commit()
            loaded = (await db.execute(select(UploadQueueItem))).scalar_one()
            assert loaded.session_id == sess.id
            assert loaded.state == "failed"
            assert loaded.attempt_count == 3
            assert loaded.next_retry_at == retry
            assert loaded.last_http_code == 503
            assert loaded.last_error == "Service Unavailable"
            assert loaded.chunks_sent == 4
            assert loaded.chunks_total == 8
            assert loaded.idempotency_key == "ulid_abc123_session_xyz"
            assert loaded.created_at is not None
            assert loaded.updated_at is not None
    finally:
        await engine.dispose()


async def test_upload_queue_item_minimal_uses_defaults(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            sess = await _seed_session(db)
            item = UploadQueueItem(
                id=uuid6.uuid7(),
                session_id=sess.id,
                state="pending",
                chunks_total=10,
                idempotency_key="ulid_minimal",
            )
            db.add(item)
            await db.commit()
            loaded = (await db.execute(select(UploadQueueItem))).scalar_one()
            assert loaded.attempt_count == 0
            assert loaded.chunks_sent == 0
            assert loaded.next_retry_at is None
            assert loaded.last_http_code is None
            assert loaded.last_error is None
    finally:
        await engine.dispose()


async def test_upload_queue_item_idempotency_key_is_unique(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            sess = await _seed_session(db)
            db.add(
                UploadQueueItem(
                    id=uuid6.uuid7(),
                    session_id=sess.id,
                    state="pending",
                    chunks_total=1,
                    idempotency_key="dup_key",
                )
            )
            await db.commit()
        async with session_factory() as db:
            db.add(
                UploadQueueItem(
                    id=uuid6.uuid7(),
                    session_id=sess.id,
                    state="pending",
                    chunks_total=1,
                    idempotency_key="dup_key",
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()
