"""T028 (sprint2-backend) — bridge link → CoachingInsight.

When POST /api/v1/bridge/links creates a TransferLink, the server
also generates a CoachingInsight summarizing the transfer findings
and points the link's `generated_report_id` at it. Bridge analysis
that doesn't surface as an insight isn't actionable — the insight
is what the rest of the coaching pipeline (timeline, list view,
notifications) keys off of.

Server-controlled, like id/created_at/driver_id: client-supplied
`generated_report_id` on the request body is ignored. The synthetic
rule_id "bridge.link_created" tags these so they're distinguishable
from rule-engine-derived insights when auditing rule health (T013).
"""

from __future__ import annotations

import asyncio
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
from sqlalchemy import select

from alembic import command
from app.deps import get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CarModel,
    CoachingInsight,
    DriverProfile,
    TrackLayout,
    TransferLink,
    User,
)
from infra.orm import (
    Session as SessionRow,
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
    return jwt.encode(claims, priv_pem, algorithm="RS256", headers={"kid": kid})


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
            "https://test.clerk", transport=httpx.MockTransport(handler)
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
            "sub": clerk_user_id,
            "email": f"{clerk_user_id}@t",
            "exp": int(time.time()) + 3600,
        },
        keys["kid"],
    )


async def _seed(factory: Any, *, clerk_user_id: str) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_a = uuid6.uuid7()
    session_b = uuid6.uuid7()
    track_a = uuid6.uuid7()
    track_b = uuid6.uuid7()
    car_a = uuid6.uuid7()
    car_b = uuid6.uuid7()
    master_track = uuid6.uuid7()
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=clerk_user_id,
                email=f"{clerk_user_id}@t",
            )
        )
        db.add(DriverProfile(id=driver_id, user_id=user_id, display_name="A"))
        for i, (tid, cid) in enumerate(
            [(track_a, car_a), (track_b, car_b)]
        ):
            db.add(
                TrackLayout(
                    id=tid,
                    source_provider=f"prov-t-{clerk_user_id}-{i}",
                    name=f"track-{clerk_user_id}-{i}",
                    layout_name="gp",
                    start_finish_lat=0.0,
                    start_finish_lon=0.0,
                    start_finish_heading_deg=0.0,
                    sector_checkpoints=[],
                    corner_names=[],
                    master_track_id=master_track,
                )
            )
            db.add(
                CarModel(
                    id=cid,
                    source_provider=f"prov-c-{clerk_user_id}-{i}",
                    name=f"car-{clerk_user_id}-{i}",
                    car_class="gt3",
                    master_car_id=uuid6.uuid7(),
                )
            )
        db.add(
            SessionRow(
                id=session_a,
                driver_id=driver_id,
                manifest={"track_id": str(track_a), "car_id": str(car_a)},
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                status="ready",
            )
        )
        db.add(
            SessionRow(
                id=session_b,
                driver_id=driver_id,
                manifest={"track_id": str(track_b), "car_id": str(car_b)},
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                status="ready",
            )
        )
        await db.commit()
    return {
        "user_id": user_id,
        "driver_id": driver_id,
        "session_pair": [session_a, session_b],
    }


def _payload(
    session_ids: list[Any], **overrides: Any
) -> dict[str, Any]:
    base = {
        "created_by": "auto",
        "session_pair": [str(s) for s in session_ids],
        "link_reason": "same_track",
        "shape_match_score": 0.78,
        "intensity_match_score": 0.62,
        "divergence_points": [],
        "transferred_skills": [],
        "non_transferred_skills": [],
        "generated_report_id": None,
    }
    base.update(overrides)
    return base


def _post(
    client: TestClient, ca: dict[str, Any], who: str, body: dict[str, Any]
) -> Any:
    return client.post(
        "/api/v1/bridge/links",
        headers={"Authorization": f"Bearer {_bearer(ca['keys'], who)}"},
        json=body,
    )


# -- behavior ------------------------------------------------------


def test_create_link_inserts_one_coaching_insight(
    configured_app: dict[str, Any],
) -> None:
    """A POST /api/v1/bridge/links must persist exactly one new
    CoachingInsight row tagged as bridge-generated. Without this,
    the link is invisible to the timeline/list views that key off
    insights."""
    seeded = asyncio.run(
        _seed(configured_app["factory"], clerk_user_id="u_a")
    )
    client = TestClient(app)
    resp = _post(
        client, configured_app, "u_a", _payload(seeded["session_pair"])
    )
    assert resp.status_code == 201

    async def _read() -> list[CoachingInsight]:
        async with configured_app["factory"]() as db:
            return list(
                (
                    await db.execute(
                        select(CoachingInsight).where(
                            CoachingInsight.driver_id == seeded["driver_id"]
                        )
                    )
                ).scalars()
            )

    insights = asyncio.run(_read())
    assert len(insights) == 1
    insight = insights[0]
    assert insight.rule_id == "bridge.link_created"
    assert insight.driver_id == seeded["driver_id"]
    # Insight points at the primary session (first in the pair) —
    # CoachingInsight has a single session_id FK; the full pair
    # is reachable via the link.
    assert insight.session_id == seeded["session_pair"][0]


def test_response_generated_report_id_points_at_the_new_insight(
    configured_app: dict[str, Any],
) -> None:
    """The link's generated_report_id field in the POST response is
    the insight's id — no follow-up GET needed for the client to
    retrieve the analysis it just triggered."""
    seeded = asyncio.run(
        _seed(configured_app["factory"], clerk_user_id="u_g")
    )
    client = TestClient(app)
    resp = _post(
        client, configured_app, "u_g", _payload(seeded["session_pair"])
    )
    assert resp.status_code == 201
    report_id = resp.json()["generated_report_id"]
    assert report_id is not None

    async def _read_insight() -> Any:
        async with configured_app["factory"]() as db:
            return (
                await db.execute(
                    select(CoachingInsight).where(
                        CoachingInsight.id == UUID(report_id)
                    )
                )
            ).scalar_one()

    insight = asyncio.run(_read_insight())
    assert insight is not None


def test_link_row_in_db_has_generated_report_id_set(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed(configured_app["factory"], clerk_user_id="u_d")
    )
    client = TestClient(app)
    _post(client, configured_app, "u_d", _payload(seeded["session_pair"]))

    async def _read_link() -> Any:
        async with configured_app["factory"]() as db:
            return (
                await db.execute(
                    select(TransferLink).where(
                        TransferLink.driver_id == seeded["driver_id"]
                    )
                )
            ).scalar_one()

    link = asyncio.run(_read_link())
    assert link.generated_report_id is not None


def test_client_supplied_generated_report_id_is_ignored(
    configured_app: dict[str, Any],
) -> None:
    """Server controls the field — like id, driver_id, created_at.
    A client-supplied value should be overwritten by the new
    insight's id, not stored verbatim."""
    seeded = asyncio.run(
        _seed(configured_app["factory"], clerk_user_id="u_o")
    )
    decoy = uuid6.uuid7()
    client = TestClient(app)
    resp = _post(
        client,
        configured_app,
        "u_o",
        _payload(seeded["session_pair"], generated_report_id=str(decoy)),
    )
    assert resp.status_code == 201
    assert resp.json()["generated_report_id"] != str(decoy)


def test_insight_severity_high_when_match_scores_low(
    configured_app: dict[str, Any],
) -> None:
    """Inverse mapping: low sim↔real match means more to coach
    about. Avg < 0.5 → high severity. This pins the heuristic so
    a future tweak doesn't silently change downstream priorities."""
    seeded = asyncio.run(
        _seed(configured_app["factory"], clerk_user_id="u_h")
    )
    client = TestClient(app)
    _post(
        client,
        configured_app,
        "u_h",
        _payload(
            seeded["session_pair"],
            shape_match_score=0.3,
            intensity_match_score=0.3,
        ),
    )

    async def _read() -> Any:
        async with configured_app["factory"]() as db:
            return (
                await db.execute(
                    select(CoachingInsight).where(
                        CoachingInsight.driver_id == seeded["driver_id"]
                    )
                )
            ).scalar_one()

    insight = asyncio.run(_read())
    assert insight.severity == "high"


def test_insight_severity_low_when_match_scores_high(
    configured_app: dict[str, Any],
) -> None:
    seeded = asyncio.run(
        _seed(configured_app["factory"], clerk_user_id="u_l")
    )
    client = TestClient(app)
    _post(
        client,
        configured_app,
        "u_l",
        _payload(
            seeded["session_pair"],
            shape_match_score=0.9,
            intensity_match_score=0.9,
        ),
    )

    async def _read() -> Any:
        async with configured_app["factory"]() as db:
            return (
                await db.execute(
                    select(CoachingInsight).where(
                        CoachingInsight.driver_id == seeded["driver_id"]
                    )
                )
            ).scalar_one()

    insight = asyncio.run(_read())
    assert insight.severity == "low"
