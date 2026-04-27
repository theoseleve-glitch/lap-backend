"""T107 (sprint2-backend) — strategy templates CRUD.

Driver-scoped library of named StrategyPlan-shaped templates the
user saves once ("my Le Mans strategy") and reuses across future
sessions. Endpoints under /api/v1/driver/strategy/templates:
  - POST   create
  - GET    list (DESC by created_at — most recent first)
  - DELETE remove

Distinct from T096's StrategyPlan: the plan is per-session,
templates are per-driver and not bound to any session — they're
the source intent users copy from when seeding a new race plan.
The shape is identical (planned_stints, planned_fuel_liters,
planned_pit_laps) plus a `name` for identification.

Pinned behaviors:
  - 401 on every endpoint when unauthenticated
  - POST 201 with the full row shape (id, created_at, name, plan
    fields)
  - POST 409 on duplicate (driver_id, name) — UNIQUE constraint
    prevents two "Le Mans 2026" templates per driver
  - POST 422 on bound violations (empty name, planned_stints<1,
    planned_fuel_liters<=0)
  - GET list returns the caller's templates ordered by
    created_at DESC — most recent first
  - GET list driver isolation: other drivers' templates absent
  - DELETE 204 on success and the row is gone
  - DELETE 404 for unknown templates AND for other drivers'
    templates (status parity prevents enumeration)
"""

from __future__ import annotations

import asyncio
import base64
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    DriverProfile,
    StrategyTemplate,
    User,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _build_keypair(kid: str = "test-kid") -> dict[str, Any]:
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pn = priv.public_key().public_numbers()
    n = pn.n.to_bytes((pn.n.bit_length() + 7) // 8, "big")
    e = pn.e.to_bytes((pn.e.bit_length() + 7) // 8, "big")
    jwk = {
        "kty": "RSA", "kid": kid, "use": "sig", "alg": "RS256",
        "n": _b64u(n), "e": _b64u(e),
    }
    return {"priv_pem": priv_pem, "kid": kid, "jwks": {"keys": [jwk]}}


def _sign(priv_pem: bytes, claims: dict[str, Any], kid: str) -> str:
    return jwt.encode(
        claims, priv_pem, algorithm="RS256", headers={"kid": kid}
    )


@pytest.fixture
def configured_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[dict[str, Any], None, None]:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    keys = _build_keypair()
    engine = make_engine(url)
    factory = make_session_factory(engine)

    async def fake_get_db() -> AsyncGenerator[Any, None]:
        async with factory() as db_session:
            yield db_session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=keys["jwks"])
        return ClerkJwksClient(
            "https://test.clerk",
            transport=httpx.MockTransport(handler),
        )

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    try:
        yield {"keys": keys, "factory": factory}
    finally:
        app.dependency_overrides.clear()


def _bearer(keys: dict[str, Any], clerk_user_id: str) -> str:
    return _sign(
        keys["priv_pem"],
        {
            "sub": clerk_user_id, "email": f"{clerk_user_id}@t",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )


async def _seed_driver(
    factory: Any, *, clerk_id: str
) -> uuid6.UUID:
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(), clerk_user_id=clerk_id,
            email=f"{clerk_id}@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(), user_id=user.id, display_name=clerk_id,
        )
        db.add_all([user, driver])
        await db.commit()
        return driver.id


async def _seed_template(
    factory: Any,
    *,
    driver_id: uuid6.UUID,
    name: str,
    created_at: datetime,
    planned_stints: int = 2,
    planned_fuel_liters: float = 80.0,
    planned_pit_laps: list[int] | None = None,
) -> uuid6.UUID:
    async with factory() as db:
        row = StrategyTemplate(
            id=uuid6.uuid7(),
            driver_id=driver_id,
            name=name,
            created_at=created_at,
            planned_stints=planned_stints,
            planned_fuel_liters=planned_fuel_liters,
            planned_pit_laps=planned_pit_laps or [30],
        )
        db.add(row)
        await db.commit()
        return row.id


def _hdrs(ca: dict[str, Any], clerk_id: str = "caller107") -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(ca['keys'], clerk_id)}"}


# -- auth --------------------------------------------------------


def test_post_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/driver/strategy/templates",
            json={
                "name": "x", "planned_stints": 1,
                "planned_fuel_liters": 50.0,
                "planned_pit_laps": [],
            },
        )
    assert resp.status_code == 401


def test_get_list_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    with TestClient(app) as client:
        resp = client.get("/api/v1/driver/strategy/templates")
    assert resp.status_code == 401


def test_delete_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/driver/strategy/templates/{uuid6.uuid7()}"
        )
    assert resp.status_code == 401


# -- POST create -------------------------------------------------


def test_post_creates_template_and_returns_full_shape(
    configured_app: dict[str, Any],
) -> None:
    """201 + body carries id, created_at (RFC3339), name, plan
    fields. Pin: response shape is the load-bearing contract."""
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller107"))
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/driver/strategy/templates",
            headers=_hdrs(configured_app),
            json={
                "name": "My Le Mans 2026",
                "planned_stints": 10,
                "planned_fuel_liters": 110.0,
                "planned_pit_laps": [22, 44, 66, 88, 110, 132, 154, 176, 198],
            },
        )
    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "My Le Mans 2026"
    assert body["planned_stints"] == 10
    assert body["planned_fuel_liters"] == 110.0
    assert body["planned_pit_laps"] == [
        22, 44, 66, 88, 110, 132, 154, 176, 198,
    ]
    assert "id" in body
    assert "created_at" in body


def test_post_duplicate_name_per_driver_returns_409(
    configured_app: dict[str, Any],
) -> None:
    """Same driver, same name → 409. Pin: UNIQUE(driver_id, name)
    prevents the user from accumulating ambiguous duplicates of
    their named playbook."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(_seed_driver(factory, clerk_id="caller107"))
    asyncio.run(_seed_template(
        factory, driver_id=driver_id, name="Spa 24h",
        created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
    ))
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/driver/strategy/templates",
            headers=_hdrs(configured_app),
            json={
                "name": "Spa 24h",
                "planned_stints": 4,
                "planned_fuel_liters": 100.0,
                "planned_pit_laps": [30, 60, 90],
            },
        )
    assert resp.status_code == 409


def test_post_duplicate_name_other_driver_allowed(
    configured_app: dict[str, Any],
) -> None:
    """Two drivers can each have a "Spa 24h" template — uniqueness
    is per-driver, not global. Pin: a global UNIQUE on name would
    let one user lock out a popular name from everyone else."""
    factory = configured_app["factory"]
    other_driver = asyncio.run(_seed_driver(factory, clerk_id="other107"))
    asyncio.run(_seed_template(
        factory, driver_id=other_driver, name="Spa 24h",
        created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
    ))
    asyncio.run(_seed_driver(factory, clerk_id="caller107"))
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/driver/strategy/templates",
            headers=_hdrs(configured_app),
            json={
                "name": "Spa 24h",
                "planned_stints": 4,
                "planned_fuel_liters": 100.0,
                "planned_pit_laps": [30, 60, 90],
            },
        )
    assert resp.status_code == 201


def test_post_rejects_empty_name(
    configured_app: dict[str, Any],
) -> None:
    """Empty name → 422. Pin: a "" template is unnamable in the
    UI list; reject at the boundary so the persisted set is
    always discriminable."""
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller107"))
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/driver/strategy/templates",
            headers=_hdrs(configured_app),
            json={
                "name": "",
                "planned_stints": 1,
                "planned_fuel_liters": 50.0,
                "planned_pit_laps": [],
            },
        )
    assert resp.status_code == 422


def test_post_rejects_zero_stints(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller107"))
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/driver/strategy/templates",
            headers=_hdrs(configured_app),
            json={
                "name": "x",
                "planned_stints": 0,
                "planned_fuel_liters": 50.0,
                "planned_pit_laps": [],
            },
        )
    assert resp.status_code == 422


# -- GET list ----------------------------------------------------


def test_get_list_empty_for_new_driver(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller107"))
    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/driver/strategy/templates",
            headers=_hdrs(configured_app),
        )
    assert resp.status_code == 200
    assert resp.json() == {"entries": []}


def test_get_list_orders_by_created_at_desc(
    configured_app: dict[str, Any],
) -> None:
    """Three templates seeded out of chronological order; the
    response must list them DESC by created_at. Pin: ASC ordering
    would put the oldest at the top — wrong "recent" semantics."""
    factory = configured_app["factory"]
    driver_id = asyncio.run(_seed_driver(factory, clerk_id="caller107"))
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    middle = asyncio.run(_seed_template(
        factory, driver_id=driver_id, name="Mid",
        created_at=base + timedelta(hours=1),
    ))
    oldest = asyncio.run(_seed_template(
        factory, driver_id=driver_id, name="Old",
        created_at=base,
    ))
    newest = asyncio.run(_seed_template(
        factory, driver_id=driver_id, name="New",
        created_at=base + timedelta(hours=2),
    ))

    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/driver/strategy/templates",
            headers=_hdrs(configured_app),
        )
    assert resp.status_code == 200
    ids = [e["id"] for e in resp.json()["entries"]]
    assert ids == [str(newest), str(middle), str(oldest)]


def test_get_list_other_drivers_templates_absent(
    configured_app: dict[str, Any],
) -> None:
    """Driver isolation: caller sees only their own templates.
    Pin: a missing driver_id filter would leak templates across
    drivers — RLS bypass per ADR-005."""
    factory = configured_app["factory"]
    caller_driver = asyncio.run(_seed_driver(factory, clerk_id="caller107"))
    other_driver = asyncio.run(_seed_driver(factory, clerk_id="other107"))
    base = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    caller_template = asyncio.run(_seed_template(
        factory, driver_id=caller_driver, name="Mine",
        created_at=base,
    ))
    other_template = asyncio.run(_seed_template(
        factory, driver_id=other_driver, name="Theirs",
        created_at=base + timedelta(hours=1),
    ))

    with TestClient(app) as client:
        resp = client.get(
            "/api/v1/driver/strategy/templates",
            headers=_hdrs(configured_app),
        )
    ids = {e["id"] for e in resp.json()["entries"]}
    assert str(caller_template) in ids
    assert str(other_template) not in ids


# -- DELETE ------------------------------------------------------


def test_delete_existing_template_returns_204_and_removes_row(
    configured_app: dict[str, Any],
) -> None:
    factory = configured_app["factory"]
    driver_id = asyncio.run(_seed_driver(factory, clerk_id="caller107"))
    template_id = asyncio.run(_seed_template(
        factory, driver_id=driver_id, name="Bye",
        created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
    ))
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/driver/strategy/templates/{template_id}",
            headers=_hdrs(configured_app),
        )
    assert resp.status_code == 204
    # Confirm it's actually gone.
    with TestClient(app) as client:
        list_resp = client.get(
            "/api/v1/driver/strategy/templates",
            headers=_hdrs(configured_app),
        )
    assert list_resp.json()["entries"] == []


def test_delete_unknown_template_returns_404(
    configured_app: dict[str, Any],
) -> None:
    asyncio.run(_seed_driver(configured_app["factory"], clerk_id="caller107"))
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/driver/strategy/templates/{uuid6.uuid7()}",
            headers=_hdrs(configured_app),
        )
    assert resp.status_code == 404


def test_delete_other_drivers_template_returns_404(
    configured_app: dict[str, Any],
) -> None:
    """IDOR: caller tries to DELETE another driver's template.
    Same 404 as 'not found' — status parity prevents enumeration.
    Pin: a missing driver_id filter would let any caller delete
    any template."""
    factory = configured_app["factory"]
    asyncio.run(_seed_driver(factory, clerk_id="caller107"))
    other_driver = asyncio.run(_seed_driver(factory, clerk_id="other107"))
    other_template = asyncio.run(_seed_template(
        factory, driver_id=other_driver, name="Theirs",
        created_at=datetime(2026, 4, 27, 12, 0, tzinfo=UTC),
    ))
    with TestClient(app) as client:
        resp = client.delete(
            f"/api/v1/driver/strategy/templates/{other_template}",
            headers=_hdrs(configured_app),
        )
    assert resp.status_code == 404
    # Confirm the row still exists (caller couldn't delete it).
    async def _still_there() -> bool:
        async with factory() as db:
            from sqlalchemy import select
            row = (
                await db.execute(
                    select(StrategyTemplate).where(
                        StrategyTemplate.id == other_template
                    )
                )
            ).scalar_one_or_none()
            return row is not None
    assert asyncio.run(_still_there())
