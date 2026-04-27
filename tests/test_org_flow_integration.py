"""T180 (sprint2-backend) — end-to-end org workflow integration test.

Walks the entire org collaboration flow through the HTTP layer:
  1. Admin creates org           → POST /api/v1/orgs
  2. Admin invites coach          → POST /api/v1/orgs/:id/invites
  3. Coach accepts invitation     → POST /api/v1/orgs/:id/invites/:t/accept
  4. Admin invites driver         → POST /api/v1/orgs/:id/invites
  5. Driver accepts invitation    → POST /api/v1/orgs/:id/invites/:t/accept
  6. Coach assigns prescription   → POST /api/v1/coach/prescriptions
  7. Driver sees the prescription → GET  /api/v1/driver/profile

Why integration?
  Per-endpoint unit tests already cover input validation, RLS, and
  status parity. They DON'T catch:
    - field name drift between OrgInvitationSchema and the body the
      accept endpoint expects
    - role-token roundtripping (the role attached at invite-time
      ends up on the OrganizationMember row at accept-time)
    - the membership state created by accept_invitation matches
      the shape the coach endpoint reads (org_id, role-as-string)
    - the prescription created by the coach endpoint surfaces in
      the driver's active_prescriptions aggregate
  These are exactly the seams that break silently when two
  routers drift apart.

The test seeds the three users (admin, coach, driver) plus the
driver's DriverProfile (created at onboarding in production —
the coach prescription endpoint requires it) and then walks the
flow purely through HTTP requests.

Pinned behaviors:
  - Full happy-path completes 7 steps with 2xx at every step
  - Final driver-side aggregate contains exactly the rx the coach
    created, with status="proposed" (not auto-accepted)
  - The coach cannot prescribe BEFORE accepting their invite
    (proves the membership-gate seam: invite ≠ membership)
"""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncGenerator, Generator
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
from infra.orm import DriverProfile, User

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
        yield {"keys": keys, "factory": factory}
    finally:
        app.dependency_overrides.clear()


def _hdrs(keys: dict[str, Any], sub: str, email: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_bearer(keys, sub, email)}"}


async def _seed_three_users_and_driver_profile(
    factory: Any,
) -> dict[str, Any]:
    """Seeds admin, coach, and driver users plus the driver's
    DriverProfile (which onboarding creates in production — the
    coach prescription endpoint requires it before assigning).
    Returns the identity tuple needed to bear JWTs for each."""
    admin_sub = "admin_t180"
    coach_sub = "coach_t180"
    driver_sub = "driver_t180"
    admin_email = "admin_t180@t.io"
    coach_email = "coach_t180@t.io"
    driver_email = "driver_t180@t.io"
    async with factory() as db:
        admin = User(
            id=uuid6.uuid7(), clerk_user_id=admin_sub, email=admin_email
        )
        coach = User(
            id=uuid6.uuid7(), clerk_user_id=coach_sub, email=coach_email
        )
        driver = User(
            id=uuid6.uuid7(),
            clerk_user_id=driver_sub,
            email=driver_email,
        )
        db.add_all([admin, coach, driver])
        await db.flush()
        db.add(
            DriverProfile(
                id=uuid6.uuid7(),
                user_id=driver.id,
                display_name="T180 Driver",
                skill_level="intermediate",
                cohort_opt_in=False,
            )
        )
        await db.commit()
        return {
            "admin": {
                "sub": admin_sub,
                "email": admin_email,
                "user_id": admin.id,
            },
            "coach": {
                "sub": coach_sub,
                "email": coach_email,
                "user_id": coach.id,
            },
            "driver": {
                "sub": driver_sub,
                "email": driver_email,
                "user_id": driver.id,
            },
        }


# -- happy path: full 7-step end-to-end ------------------------------


async def test_full_org_flow_admin_invite_coach_prescribe_driver_sees(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    actors = await _seed_three_users_and_driver_profile(factory)
    admin = actors["admin"]
    coach = actors["coach"]
    driver = actors["driver"]

    client = TestClient(app)

    # Step 1: admin creates org
    create_resp = client.post(
        "/api/v1/orgs",
        json={"name": "Acme Racing", "plan": "team", "seats_limit": 10},
        headers=_hdrs(keys, admin["sub"], admin["email"]),
    )
    assert create_resp.status_code == 201, create_resp.text
    org_id = create_resp.json()["id"]
    assert create_resp.json()["admin_user_id"] == str(admin["user_id"])

    # Step 2: admin invites coach (role=coach)
    invite_coach_resp = client.post(
        f"/api/v1/orgs/{org_id}/invites",
        json={"email": coach["email"], "role": "coach"},
        headers=_hdrs(keys, admin["sub"], admin["email"]),
    )
    assert invite_coach_resp.status_code == 201, invite_coach_resp.text
    coach_token = invite_coach_resp.json()["token"]
    assert invite_coach_resp.json()["role"] == "coach"

    # Step 3: coach accepts invitation
    accept_coach_resp = client.post(
        f"/api/v1/orgs/{org_id}/invites/{coach_token}/accept",
        headers=_hdrs(keys, coach["sub"], coach["email"]),
    )
    assert accept_coach_resp.status_code == 201, accept_coach_resp.text
    assert accept_coach_resp.json()["role"] == "coach"
    assert accept_coach_resp.json()["user_id"] == str(coach["user_id"])

    # Step 4: admin invites driver (role=driver)
    invite_driver_resp = client.post(
        f"/api/v1/orgs/{org_id}/invites",
        json={"email": driver["email"], "role": "driver"},
        headers=_hdrs(keys, admin["sub"], admin["email"]),
    )
    assert invite_driver_resp.status_code == 201, invite_driver_resp.text
    driver_token = invite_driver_resp.json()["token"]

    # Step 5: driver accepts invitation
    accept_driver_resp = client.post(
        f"/api/v1/orgs/{org_id}/invites/{driver_token}/accept",
        headers=_hdrs(keys, driver["sub"], driver["email"]),
    )
    assert accept_driver_resp.status_code == 201, accept_driver_resp.text
    assert accept_driver_resp.json()["role"] == "driver"

    # Step 6: coach assigns a prescription to driver
    rx_resp = client.post(
        "/api/v1/coach/prescriptions",
        json={
            "org_id": org_id,
            "driver_user_id": str(driver["user_id"]),
            "priority": "p1",
            "title": "Trail-brake into T1",
            "rationale": (
                "Watching your sectors, you scrub speed mid-corner."
                " Try carrying brake pressure to the apex."
            ),
            "drill_template_id": None,
        },
        headers=_hdrs(keys, coach["sub"], coach["email"]),
    )
    assert rx_resp.status_code == 201, rx_resp.text
    rx_id = rx_resp.json()["id"]
    assert rx_resp.json()["status"] == "proposed"
    assert rx_resp.json()["title"] == "Trail-brake into T1"

    # Step 7: driver fetches their profile aggregate, sees the rx
    profile_resp = client.get(
        "/api/v1/driver/profile",
        headers=_hdrs(keys, driver["sub"], driver["email"]),
    )
    assert profile_resp.status_code == 200, profile_resp.text
    active = profile_resp.json()["active_prescriptions"]
    rx_ids = [item["id"] for item in active]
    assert rx_id in rx_ids, (
        "coach-assigned prescription not visible to driver"
    )
    matching = next(item for item in active if item["id"] == rx_id)
    assert matching["status"] == "proposed"
    assert matching["title"] == "Trail-brake into T1"


# -- seam test: coach must accept invite before prescribing ----------


async def test_coach_cannot_prescribe_before_accepting_invite(
    configured_app: dict[str, Any],
) -> None:
    """An outstanding invitation does not equal membership.
    Until the coach hits the accept endpoint, they have no
    OrganizationMember row, and the coach prescription endpoint
    must 403. This proves invite-state and membership-state are
    the two distinct things they need to be."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    actors = await _seed_three_users_and_driver_profile(factory)
    admin = actors["admin"]
    coach = actors["coach"]
    driver = actors["driver"]

    client = TestClient(app)

    create_resp = client.post(
        "/api/v1/orgs",
        json={"name": "Acme Racing", "plan": "team", "seats_limit": 10},
        headers=_hdrs(keys, admin["sub"], admin["email"]),
    )
    assert create_resp.status_code == 201
    org_id = create_resp.json()["id"]

    # Admin invites coach (token issued, NOT accepted)
    invite_coach_resp = client.post(
        f"/api/v1/orgs/{org_id}/invites",
        json={"email": coach["email"], "role": "coach"},
        headers=_hdrs(keys, admin["sub"], admin["email"]),
    )
    assert invite_coach_resp.status_code == 201

    # Driver accepts so the prescription target is a valid member
    invite_driver_resp = client.post(
        f"/api/v1/orgs/{org_id}/invites",
        json={"email": driver["email"], "role": "driver"},
        headers=_hdrs(keys, admin["sub"], admin["email"]),
    )
    driver_token = invite_driver_resp.json()["token"]
    accept_driver_resp = client.post(
        f"/api/v1/orgs/{org_id}/invites/{driver_token}/accept",
        headers=_hdrs(keys, driver["sub"], driver["email"]),
    )
    assert accept_driver_resp.status_code == 201

    # Coach has an outstanding invite but no membership: must 403
    rx_resp = client.post(
        "/api/v1/coach/prescriptions",
        json={
            "org_id": org_id,
            "driver_user_id": str(driver["user_id"]),
            "priority": "p1",
            "title": "Premature drill",
            "rationale": "Should not be visible — coach not a member yet.",
            "drill_template_id": None,
        },
        headers=_hdrs(keys, coach["sub"], coach["email"]),
    )
    assert rx_resp.status_code == 403, rx_resp.text
    assert rx_resp.json()["detail"] == "not_authorized"
