"""T224 / WA-009 — `GET /api/v1/sessions/:id/next_actions`.

Returns the post-session debrief bundle: WeaknessEpisodes
touched by this session + ActionPrescriptions linked to those
episodes (still in the actionable lifecycle).

Replaces the frontend's old SessionDebrief client-side
heuristic — copy comes from server-rendered template fields,
not from string-stitching in JS.

Auth posture matches `sessions.get_session`: 404 collapses
unknown + not-owned so session_ids aren't probeable across
users.

Episode membership uses Python-side filtering on
`sessions_affected` to stay portable across SQLite (tests) and
Postgres (prod) — JSONB containment ops aren't standard SQL.
Per-driver episode counts are bounded so the post-filter cost
is dwarfed by the round-trip.
"""

import base64
import time
from collections.abc import AsyncGenerator, Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import jwt
import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.config import Config as AppConfig
from app.deps import get_config, get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionPrescription,
    DriverProfile,
    Session,
    User,
    WeaknessEpisode,
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


def _bearer(keys: dict[str, Any], sub: str, email: str) -> str:
    return jwt.encode(
        {"sub": sub, "email": email, "exp": int(time.time()) + 3600},
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
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")

    keys = _build_keypair()
    engine = make_engine(url)
    factory = make_session_factory(engine)

    async def fake_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    def fake_jwks_client() -> ClerkJwksClient:
        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=keys["jwks"])

        return ClerkJwksClient(
            "https://test.clerk", transport=httpx.MockTransport(handler)
        )

    def fake_config() -> AppConfig:
        return AppConfig()

    app.dependency_overrides[get_db] = fake_get_db
    app.dependency_overrides[get_jwks_client] = fake_jwks_client
    app.dependency_overrides[get_config] = fake_config
    try:
        yield {"keys": keys, "factory": factory}
    finally:
        app.dependency_overrides.clear()


async def _seed_user_with_session(
    factory: Any, *, sub: str, email: str
) -> tuple[UUID, UUID, UUID]:
    """Returns (user_id, driver_id, session_id)."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_id = uuid6.uuid7()
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(User(id=user_id, clerk_user_id=sub, email=email))
        db.add(
            DriverProfile(
                id=driver_id, user_id=user_id, display_name="A"
            )
        )
        db.add(
            Session(
                id=session_id, driver_id=driver_id,
                manifest={}, status="ready", created_at=now,
            )
        )
        await db.commit()
    return user_id, driver_id, session_id


async def _add_episode(
    factory: Any,
    *,
    driver_id: UUID,
    sessions_affected: list[str],
    library_rule_id: str = "br.late_braking.apex_speed_loss",
) -> UUID:
    eid = uuid6.uuid7()
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(
            WeaknessEpisode(
                id=eid, driver_id=driver_id,
                category="technique", subcategory="late_braking",
                first_observed_at=now, last_observed_at=now,
                recurrence_count=len(sessions_affected),
                sessions_affected=sessions_affected,
                severity="medium", confidence=1.0, status="open",
                linked_prescriptions=[], linked_insights=[],
                library_rule_id=library_rule_id,
            )
        )
        await db.commit()
    return eid


async def _add_prescription(
    factory: Any,
    *,
    driver_id: UUID,
    episode_id: UUID,
    status: str = "proposed",
) -> UUID:
    pid = uuid6.uuid7()
    now = datetime.now(UTC)
    async with factory() as db:
        db.add(
            ActionPrescription(
                id=pid, driver_id=driver_id,
                weakness_episode_id=episode_id, created_at=now,
                priority="p1", title="Address late braking",
                rationale="r", expected_context={},
                success_condition={}, status=status,
                drill_template_id="drill.x",
            )
        )
        await db.commit()
    return pid


def test_next_actions_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    fake_id = uuid6.uuid7()
    resp = TestClient(app).get(
        f"/api/v1/sessions/{fake_id}/next_actions"
    )
    assert resp.status_code == 401


async def test_next_actions_unknown_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id="u_na_no",
                email="no@test",
            )
        )
        await db.commit()
    token = _bearer(keys, "u_na_no", "no@test")
    fake_id = uuid6.uuid7()
    resp = TestClient(app).get(
        f"/api/v1/sessions/{fake_id}/next_actions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_next_actions_other_drivers_session_returns_404(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, _, session_a = await _seed_user_with_session(
        factory, sub="u_na_a", email="na_a@test"
    )
    async with factory() as db:
        u = uuid6.uuid7()
        db.add(User(id=u, clerk_user_id="u_na_b", email="na_b@test"))
        db.add(
            DriverProfile(id=uuid6.uuid7(), user_id=u, display_name="B")
        )
        await db.commit()
    token = _bearer(keys, "u_na_b", "na_b@test")
    resp = TestClient(app).get(
        f"/api/v1/sessions/{session_a}/next_actions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404


async def test_next_actions_returns_empty_arrays_when_nothing_to_do(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, _, session_id = await _seed_user_with_session(
        factory, sub="u_na_empty", email="na_empty@test"
    )
    token = _bearer(keys, "u_na_empty", "na_empty@test")
    resp = TestClient(app).get(
        f"/api/v1/sessions/{session_id}/next_actions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == str(session_id)
    assert body["weakness_episodes"] == []
    assert body["action_prescriptions"] == []


async def test_next_actions_returns_episode_touched_by_session(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, session_id = await _seed_user_with_session(
        factory, sub="u_na_touch", email="na_touch@test"
    )
    eid = await _add_episode(
        factory, driver_id=driver_id,
        sessions_affected=[str(session_id)],
    )
    token = _bearer(keys, "u_na_touch", "na_touch@test")
    resp = TestClient(app).get(
        f"/api/v1/sessions/{session_id}/next_actions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = [e["id"] for e in body["weakness_episodes"]]
    assert ids == [str(eid)]


async def test_next_actions_excludes_episodes_from_other_sessions(
    configured_app: dict[str, Any],
) -> None:
    """Episodes that don't list this session_id in sessions_affected
    must not surface — even if they belong to the same driver."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, session_id = await _seed_user_with_session(
        factory, sub="u_na_excl", email="na_excl@test"
    )
    other_session = uuid6.uuid7()
    eid_other = await _add_episode(
        factory, driver_id=driver_id,
        sessions_affected=[str(other_session)],
    )
    token = _bearer(keys, "u_na_excl", "na_excl@test")
    resp = TestClient(app).get(
        f"/api/v1/sessions/{session_id}/next_actions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    ids = [e["id"] for e in body["weakness_episodes"]]
    assert str(eid_other) not in ids
    assert ids == []


async def test_next_actions_returns_open_prescriptions_for_episodes(
    configured_app: dict[str, Any],
) -> None:
    """Prescriptions linked to a touched episode surface, but only
    in the open lifecycle (proposed/accepted/in_progress)."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    _, driver_id, session_id = await _seed_user_with_session(
        factory, sub="u_na_pres", email="na_pres@test"
    )
    eid = await _add_episode(
        factory, driver_id=driver_id,
        sessions_affected=[str(session_id)],
    )
    p_open = await _add_prescription(
        factory, driver_id=driver_id, episode_id=eid,
        status="proposed",
    )
    await _add_prescription(
        factory, driver_id=driver_id, episode_id=eid,
        status="skipped",
    )
    token = _bearer(keys, "u_na_pres", "na_pres@test")
    resp = TestClient(app).get(
        f"/api/v1/sessions/{session_id}/next_actions",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    pres_ids = [p["id"] for p in body["action_prescriptions"]]
    assert pres_ids == [str(p_open)]
