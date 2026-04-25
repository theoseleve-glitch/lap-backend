from collections.abc import AsyncGenerator, Generator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.deps import get_db
from app.main import app
from infra.db import make_engine, make_session_factory
from infra.orm import User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def configured_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[str, Any], None, None]:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    engine = make_engine(url)
    factory = make_session_factory(engine)

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = fake_get_db
    try:
        yield {"factory": factory, "url": url}
    finally:
        app.dependency_overrides.clear()


def _user_created_payload(
    clerk_id: str, email: str, email_id: str = "idn_1"
) -> dict[str, Any]:
    return {
        "object": "event",
        "type": "user.created",
        "data": {
            "id": clerk_id,
            "primary_email_address_id": email_id,
            "email_addresses": [
                {"id": email_id, "email_address": email},
            ],
        },
    }


def _user_deleted_payload(clerk_id: str) -> dict[str, Any]:
    return {
        "object": "event",
        "type": "user.deleted",
        "data": {"id": clerk_id, "deleted": True},
    }


async def test_user_created_event_creates_user_row(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/webhooks",
        json=_user_created_payload("user_clerk_a", "a@test"),
    )
    assert resp.status_code == 200

    async with factory() as session:
        rows = (
            await session.execute(
                select(User).where(User.clerk_user_id == "user_clerk_a")
            )
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].email == "a@test"


async def test_user_created_is_idempotent(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    client = TestClient(app)
    payload = _user_created_payload("user_clerk_dup", "dup@test")
    r1 = client.post("/api/v1/auth/webhooks", json=payload)
    r2 = client.post("/api/v1/auth/webhooks", json=payload)
    assert r1.status_code == 200
    assert r2.status_code == 200

    async with factory() as session:
        rows = (
            await session.execute(
                select(User).where(User.clerk_user_id == "user_clerk_dup")
            )
        ).scalars().all()
        assert len(rows) == 1


async def test_user_deleted_event_removes_user_row(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    client = TestClient(app)
    client.post(
        "/api/v1/auth/webhooks",
        json=_user_created_payload("user_to_delete", "del@test"),
    )

    resp = client.post(
        "/api/v1/auth/webhooks",
        json=_user_deleted_payload("user_to_delete"),
    )
    assert resp.status_code == 200

    async with factory() as session:
        rows = (
            await session.execute(
                select(User).where(User.clerk_user_id == "user_to_delete")
            )
        ).scalars().all()
        assert rows == []


@pytest.mark.usefixtures("configured_app")
def test_user_deleted_for_unknown_user_returns_200() -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/webhooks",
        json=_user_deleted_payload("user_never_existed"),
    )
    assert resp.status_code == 200


async def test_unknown_event_type_is_no_op(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/webhooks",
        json={
            "object": "event",
            "type": "session.created",
            "data": {"id": "sess_1"},
        },
    )
    assert resp.status_code == 200

    async with factory() as session:
        rows = (await session.execute(select(User))).scalars().all()
        assert rows == []


@pytest.mark.usefixtures("configured_app")
def test_malformed_payload_returns_400() -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/webhooks",
        json={"not_an_event": True},
    )
    assert resp.status_code == 400


@pytest.mark.usefixtures("configured_app")
def test_user_created_with_no_email_returns_400() -> None:
    client = TestClient(app)
    resp = client.post(
        "/api/v1/auth/webhooks",
        json={
            "object": "event",
            "type": "user.created",
            "data": {
                "id": "user_no_email",
                "primary_email_address_id": None,
                "email_addresses": [],
            },
        },
    )
    assert resp.status_code == 400
