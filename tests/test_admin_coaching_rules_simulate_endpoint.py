"""T201 — `POST /api/v1/admin/coaching/rules/simulate`.

Admin-only dry-run for the coaching rules engine. Operator pastes
a feature context (the dict shape `coaching.library.rule_runner.run_rules`
expects), and the endpoint loads every YAML under
`coaching/library/rules/`, evaluates each rule, and returns the
resulting `CoachingInsight` list as JSON.

Why this matters: a typo in an `explanation_template` (`{{coner}}`
instead of `{{corner}}`) won't fail schema validation — it
explodes at render-time when the missing key bubbles as
`InsightRenderError`. Without a dry-run endpoint that surface
isn't visible until a real driver session triggers it. Simulate
turns "rule edit → 422 with rule_id and missing var" into a
sub-second loop.
"""

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
from sqlalchemy.ext.asyncio import AsyncSession

from alembic import command
from app.config import Config as AppConfig
from app.deps import get_config, get_db, get_jwks_client
from app.main import app
from infra.clerk import ClerkJwksClient
from infra.db import make_engine, make_session_factory
from infra.orm import User

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


async def _seed_admin(factory: Any, sub: str, email: str) -> None:
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id=sub,
                email=email,
                is_admin=True,
            )
        )
        await db.commit()


# A context tuned to fire 3 of the 10 braking rules — same triple
# the Red Bull Ring fixture (T200) is designed for: T188 late
# braking, T191 inconsistent pressure, T195 rear/front imbalance.
# Used across the "happy path" tests to keep them all asserting
# the same rule set.
_FIRING_CONTEXT: dict[str, Any] = {
    "apex_speed": 91.0,
    "reference_apex_speed": 100.0,
    "entry_speed": 96.0,
    "reference_entry_speed": 100.0,
    "brake_release_rate": 2.5,
    "reference_brake_release_rate": 2.0,
    "brake_pressure_cov_in_sector": 0.20,
    "lockup_event_count": 2,
    "first_lap_brake_distance_ratio": 1.05,
    "mid_corner_brake_pressure_pct": 0.10,
    "rear_front_slip_ratio_entry": 1.70,
    "brake_release_position_pct": 0.78,
    "peak_decel_grip_ratio": 0.92,
    # Template vars the firing rules' beginner explanations need.
    "delta_kmh": 9,
    "corner": "Turn 1",
    "sector": "S1",
    "cov_pct": 20,
    "ratio_pct": 170,
    # T074 endurance signals at preservation values (rules silent).
    # Without these the runner raises KeyError when evaluating any
    # endurance rule against this braking-only context.
    "stint_pace_decay_seconds_per_lap": 0.0,
    "stint_tire_preservation_score": 1.0,
    "stint_closing_window_consistency_seconds": 0.0,
    "stint_fuel_used_per_lap_liters": 0.0,
    "stint_lap_count": 100,
    # T091 target-preparation signal at sprint-only sentinel
    # (999 = "no event pinned, far future"). Same reason as the
    # endurance signals: any rule referencing this name will
    # KeyError if the context omits it.
    "sessions_until_target_event": 999,
    # T104 strategy-execution signals at "good enough" values so
    # `strat.execution.low_score` stays silent in the firing
    # context (this fixture targets braking-rule firing, not
    # strategy). Same KeyError-avoidance reason as above —
    # anything the rule's condition or template references must
    # be present.
    "strategy_execution_score": 100,
    "pit_lap_delta_total": 0,
    "fuel_delta_liters": 0.0,
    "stint_duration_delta_laps": 0,
    # T111 throttle-pickup signals at quiet values so
    # `th.early_throttle.snap_oversteer` stays silent in this
    # braking-focused fixture.
    "throttle_pct_at_apex": 0.20,
    "peak_rear_slip_angle_deg": 3.0,
    # T112 late-throttle signals at on-pace values so
    # `th.late_throttle.exit_speed_loss` stays silent.
    "exit_speed": 99.0,
    "reference_exit_speed": 100.0,
    "exit_speed_delta_kmh": 1,
    # T113 abrupt-pickup signals at modulated values so
    # `th.abrupt_pickup.traction_break` stays silent.
    "throttle_application_rate_pct_per_s": 150,
    "traction_break_event_count": 0,
    # T114 partial-throttle signals at clean-commitment values
    # so `th.partial_throttle.mid_corner` stays silent.
    "partial_throttle_duration_at_apex_ms": 50,
    "coast_time_pct_in_corner": 0.05,
    # T115 throttle-lift signals at no-event values so
    # `th.throttle_lift.mid_corner` stays silent.
    "mid_corner_lift_event_count": 0,
    "lift_throttle_drop_pct": 0.05,
    # T116 staircase-pickup signals at smooth-ramp values so
    # `th.staircase_pattern.uneven_modulation` stays silent.
    "throttle_step_count_in_pickup": 1,
    "throttle_pickup_smoothness_index": 0.85,
    # T117 early-full / pre-apex signals at apex-timed values so
    # `th.early_full.before_apex` stays silent.
    "pre_apex_throttle_peak_pct": 0.50,
    "full_throttle_reached_pct_before_apex": 0.02,
    # T118 straight-line throttle utilization at clean values
    # so `th.full_throttle_underuse.straights` stays silent.
    "straight_full_throttle_time_pct": 0.95,
    "straight_length_m": 400,
}

# A context where every braking rule should be silent — values
# safely on the non-firing side of every threshold. Used to
# assert empty-list behavior.
_QUIET_CONTEXT: dict[str, Any] = {
    "apex_speed": 99.0,
    "reference_apex_speed": 100.0,
    "entry_speed": 99.0,
    "reference_entry_speed": 100.0,
    "brake_release_rate": 2.0,
    "reference_brake_release_rate": 2.0,
    "brake_pressure_cov_in_sector": 0.05,
    "lockup_event_count": 0,
    "first_lap_brake_distance_ratio": 1.02,
    "mid_corner_brake_pressure_pct": 0.05,
    "rear_front_slip_ratio_entry": 1.10,
    "brake_release_position_pct": 0.85,
    "peak_decel_grip_ratio": 0.95,
    # T074 endurance signals at preservation values (rules silent).
    "stint_pace_decay_seconds_per_lap": 0.0,
    "stint_tire_preservation_score": 1.0,
    "stint_closing_window_consistency_seconds": 0.0,
    "stint_fuel_used_per_lap_liters": 0.0,
    "stint_lap_count": 100,
    # T091 target-preparation signal at sprint-only sentinel.
    "sessions_until_target_event": 999,
    # T104 strategy-execution signals at "good enough" values so
    # `strat.execution.low_score` stays silent in the quiet context.
    "strategy_execution_score": 100,
    "pit_lap_delta_total": 0,
    "fuel_delta_liters": 0.0,
    "stint_duration_delta_laps": 0,
    # T111 throttle-pickup signals at quiet values so
    # `th.early_throttle.snap_oversteer` stays silent.
    "throttle_pct_at_apex": 0.20,
    "peak_rear_slip_angle_deg": 3.0,
    # T112 late-throttle signals at on-pace values so
    # `th.late_throttle.exit_speed_loss` stays silent.
    "exit_speed": 99.0,
    "reference_exit_speed": 100.0,
    "exit_speed_delta_kmh": 1,
    # T113 abrupt-pickup signals at modulated values so
    # `th.abrupt_pickup.traction_break` stays silent.
    "throttle_application_rate_pct_per_s": 150,
    "traction_break_event_count": 0,
    # T114 partial-throttle signals at clean-commitment values
    # so `th.partial_throttle.mid_corner` stays silent.
    "partial_throttle_duration_at_apex_ms": 50,
    "coast_time_pct_in_corner": 0.05,
    # T115 throttle-lift signals at no-event values so
    # `th.throttle_lift.mid_corner` stays silent.
    "mid_corner_lift_event_count": 0,
    "lift_throttle_drop_pct": 0.05,
    # T116 staircase-pickup signals at smooth-ramp values so
    # `th.staircase_pattern.uneven_modulation` stays silent.
    "throttle_step_count_in_pickup": 1,
    "throttle_pickup_smoothness_index": 0.85,
    # T117 early-full / pre-apex signals at apex-timed values so
    # `th.early_full.before_apex` stays silent.
    "pre_apex_throttle_peak_pct": 0.50,
    "full_throttle_reached_pct_before_apex": 0.02,
    # T118 straight-line throttle utilization at clean values
    # so `th.full_throttle_underuse.straights` stays silent.
    "straight_full_throttle_time_pct": 0.95,
    "straight_length_m": 400,
}


def test_simulate_unauthenticated_returns_401(
    configured_app: dict[str, Any],  # noqa: ARG001
) -> None:
    resp = TestClient(app).post(
        "/api/v1/admin/coaching/rules/simulate",
        json={"context": _FIRING_CONTEXT},
    )
    assert resp.status_code == 401


async def test_simulate_non_admin_returns_403(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    async with factory() as db:
        db.add(
            User(
                id=uuid6.uuid7(),
                clerk_user_id="u_plain_sim",
                email="plain_sim@test",
                is_admin=False,
            )
        )
        await db.commit()
    token = _bearer(keys, "u_plain_sim", "plain_sim@test")
    resp = TestClient(app).post(
        "/api/v1/admin/coaching/rules/simulate",
        headers={"Authorization": f"Bearer {token}"},
        json={"context": _FIRING_CONTEXT},
    )
    assert resp.status_code == 403


async def test_simulate_with_firing_context_returns_insights(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_sim_fire", "admin_sim_fire@test")
    token = _bearer(keys, "u_admin_sim_fire", "admin_sim_fire@test")

    resp = TestClient(app).post(
        "/api/v1/admin/coaching/rules/simulate",
        headers={"Authorization": f"Bearer {token}"},
        json={"context": _FIRING_CONTEXT},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "insights" in body
    fired_ids = {ins["rule_id"] for ins in body["insights"]}
    assert {
        "br.late_braking.apex_speed_loss",
        "br.inconsistent_pressure.sector_variance",
        "br.balance_shift.rear_locking_entry",
    }.issubset(fired_ids)
    # Every insight serialized must carry a rendered explanation
    # with no leftover {{var}} placeholders.
    for ins in body["insights"]:
        assert ins["explanation"]
        assert "{{" not in ins["explanation"]
        assert ins["category"] == "technique"
        assert ins["difficulty"] == "beginner"
        assert isinstance(ins["rule_version"], int)


async def test_simulate_with_quiet_context_returns_empty_list(
    configured_app: dict[str, Any],
) -> None:
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_sim_quiet", "admin_sim_quiet@test")
    token = _bearer(keys, "u_admin_sim_quiet", "admin_sim_quiet@test")

    resp = TestClient(app).post(
        "/api/v1/admin/coaching/rules/simulate",
        headers={"Authorization": f"Bearer {token}"},
        json={"context": _QUIET_CONTEXT},
    )
    assert resp.status_code == 200
    assert resp.json() == {"insights": []}


async def test_simulate_passes_through_difficulty(
    configured_app: dict[str, Any],
) -> None:
    """The intermediate templates differ word-for-word from
    beginner — assert the response carries the requested level
    AND that the explanation text has flipped accordingly."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_sim_diff", "admin_sim_diff@test")
    token = _bearer(keys, "u_admin_sim_diff", "admin_sim_diff@test")

    # Minimal firing context: only T188 late braking; need its
    # intermediate template vars (delta_kmh, corner) — both already
    # in _FIRING_CONTEXT.
    resp = TestClient(app).post(
        "/api/v1/admin/coaching/rules/simulate",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "context": _FIRING_CONTEXT,
            "difficulty": "intermediate",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    late_braking = next(
        ins
        for ins in body["insights"]
        if ins["rule_id"] == "br.late_braking.apex_speed_loss"
    )
    assert late_braking["difficulty"] == "intermediate"
    # Beginner says "You lose…", intermediate says "Apex speed deficit…"
    assert "Apex speed deficit" in late_braking["explanation"]


async def test_simulate_missing_template_var_returns_422(
    configured_app: dict[str, Any],
) -> None:
    """If the context fires a rule but lacks one of the rule's
    template vars, return 422 with the rule_id and the missing
    var name. This is the whole point of the endpoint: surface
    rule-edit bugs before they reach a driver session."""
    keys = configured_app["keys"]
    factory = configured_app["factory"]
    await _seed_admin(factory, "u_admin_sim_422", "admin_sim_422@test")
    token = _bearer(keys, "u_admin_sim_422", "admin_sim_422@test")

    # Drop `corner` from the firing context: T188's beginner
    # template references it, so render must fail.
    bad_context = {k: v for k, v in _FIRING_CONTEXT.items() if k != "corner"}
    resp = TestClient(app).post(
        "/api/v1/admin/coaching/rules/simulate",
        headers={"Authorization": f"Bearer {token}"},
        json={"context": bad_context},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    # Both T188 (late_braking) and T195 (balance_shift) reference
    # `corner` in their explanation_template; whichever the runner
    # evaluates first will be the one that surfaces. Either is a
    # correct diagnostic — the operator only needs ONE rule named
    # to find the broken context. Yaml walk is sorted alphabetically.
    assert "corner" in detail
    assert any(
        rule_id in detail
        for rule_id in (
            "br.late_braking.apex_speed_loss",
            "br.balance_shift.rear_locking_entry",
        )
    )
