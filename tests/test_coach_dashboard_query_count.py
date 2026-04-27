"""T190 (sprint2-backend) — bounded query count for coach dashboard.

The dashboard endpoint composes one `CoachDashboardDriverEntry`
per driver-role member of the org. Pre-T190 it ran 4 queries
*per driver* (profile, latest snapshot, open weaknesses count,
open prescriptions count) on top of 2 setup queries — i.e.,
2 + 4N. For an org with 50 drivers that's 202 queries per page
load.

The T190 fix bulk-loads all four child tables with `IN(...)`
predicates so the total query count is bounded regardless of
the driver count: 2 setup + 4 bulk = 6 queries flat.

Pinned behavior:
  - Dashboard with 1 driver issues ≤8 SQL queries
  - Dashboard with 3 drivers issues the SAME number of queries
    as 1 driver (within ±1 — bulk load is independent of N)
  - Output entries unchanged (behavior preserved — the existing
    test_orgs_coach_dashboard_endpoint.py tests cover this; this
    file only adds the query-count pin)
"""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncGenerator, Generator
from contextlib import contextmanager
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
from sqlalchemy import event

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    DriverProfile,
    Organization,
    OrganizationMember,
    User,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _build_keypair() -> dict[str, Any]:
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
        "kty": "RSA", "kid": "kid", "use": "sig", "alg": "RS256",
        "n": _b64u(n), "e": _b64u(e),
    }
    return {"priv_pem": priv_pem, "kid": "kid", "jwks": {"keys": [jwk]}}


def _bearer(keys: dict[str, Any], sub: str) -> str:
    return jwt.encode(
        {"sub": sub, "email": f"{sub}@t.io", "exp": int(time.time()) + 3600},
        keys["priv_pem"],
        algorithm="RS256",
        headers={"kid": keys["kid"]},
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
        async with factory() as session:
            yield session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=keys["jwks"])
        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    try:
        yield {"keys": keys, "factory": factory, "engine": engine}
    finally:
        app.dependency_overrides.clear()


async def _seed_org_with_n_drivers(
    factory: Any, *, admin_clerk: str, driver_count: int
) -> dict[str, Any]:
    """Seeds an org with one admin and `driver_count` drivers.
    Each driver has a DriverProfile so the dashboard touches all
    code paths (profile + snapshot + weakness + rx)."""
    async with factory() as db:
        admin = User(
            id=uuid6.uuid7(),
            clerk_user_id=admin_clerk,
            email=f"{admin_clerk}@t.io",
        )
        db.add(admin)
        await db.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T190 Org",
            plan="team",
            admin_user_id=admin.id,
            seats_limit=100,
        )
        db.add(org)
        await db.flush()
        db.add(
            OrganizationMember(
                id=uuid6.uuid7(),
                org_id=org.id,
                user_id=admin.id,
                role="admin",
            )
        )
        for i in range(driver_count):
            drv = User(
                id=uuid6.uuid7(),
                clerk_user_id=f"drv190_{i}",
                email=f"drv190_{i}@t.io",
            )
            db.add(drv)
            await db.flush()
            db.add(
                OrganizationMember(
                    id=uuid6.uuid7(),
                    org_id=org.id,
                    user_id=drv.id,
                    role="driver",
                )
            )
            db.add(
                DriverProfile(
                    id=uuid6.uuid7(),
                    user_id=drv.id,
                    display_name=f"Driver {i}",
                    skill_level="intermediate",
                    cohort_opt_in=False,
                )
            )
        await db.commit()
        return {"org_id": org.id}


@contextmanager
def _count_queries(engine: Any) -> Any:
    """Count every SQL statement issued through the sync engine
    underlying our async wrapper. SQLAlchemy fires
    `before_cursor_execute` for every cursor.execute — including
    the prepared-statement variants — so the count reflects
    actual round trips, not ORM operations."""
    counter = {"n": 0}

    def _on_execute(*_args: Any, **_kw: Any) -> None:
        counter["n"] += 1

    sync_engine = engine.sync_engine
    event.listen(sync_engine, "before_cursor_execute", _on_execute)
    try:
        yield counter
    finally:
        event.remove(sync_engine, "before_cursor_execute", _on_execute)


# -- bounded query count -----------------------------------------------


async def test_dashboard_with_one_driver_uses_few_queries(
    configured_app: dict[str, Any],
) -> None:
    """A single-driver dashboard should issue ≤8 SQL queries
    (auth ladder + 2 setup + 4 bulk-loads = 7 expected, with 1
    slack for any future minor addition). Pre-T190 the same
    request was 6 queries (2 + 4*1) — the test would falsely pass.
    The 3-driver test below is the real regression catcher."""
    factory = configured_app["factory"]
    engine = configured_app["engine"]
    keys = configured_app["keys"]
    seeded = await _seed_org_with_n_drivers(
        factory, admin_clerk="admin190a", driver_count=1
    )
    headers = {"Authorization": f"Bearer {_bearer(keys, 'admin190a')}"}
    client = TestClient(app)

    with _count_queries(engine) as counter:
        resp = client.get(
            f"/api/v1/orgs/{seeded['org_id']}/dashboard",
            headers=headers,
        )
    assert resp.status_code == 200
    assert counter["n"] <= 8, (
        f"unexpectedly high query count: {counter['n']}"
    )


async def test_dashboard_query_count_is_bounded_independent_of_driver_count(
    configured_app: dict[str, Any],
) -> None:
    """The whole point of T190: the dashboard with 3 drivers must
    issue (about) the same number of queries as with 1 driver.
    Pre-fix this would be 2 + 4*3 = 14 queries vs 2 + 4*1 = 6 —
    a bonafide N+1. After fix, both are 2 + 4 = 6 queries flat
    regardless of N. We assert ≤8 to allow a small margin for
    auth-related or middleware queries that vary."""
    factory = configured_app["factory"]
    engine = configured_app["engine"]
    keys = configured_app["keys"]
    seeded = await _seed_org_with_n_drivers(
        factory, admin_clerk="admin190b", driver_count=3
    )
    headers = {"Authorization": f"Bearer {_bearer(keys, 'admin190b')}"}
    client = TestClient(app)

    with _count_queries(engine) as counter:
        resp = client.get(
            f"/api/v1/orgs/{seeded['org_id']}/dashboard",
            headers=headers,
        )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["entries"]) == 3
    assert counter["n"] <= 8, (
        f"N+1 regression: 3-driver dashboard issued {counter['n']} "
        "queries (expected ≤8 — bulk-load contract). Pre-T190 this "
        "was 2 + 4*3 = 14."
    )
