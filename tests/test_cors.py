"""CORS posture tests — pinned to the env-driven origin list.

T184 makes the prod allowed-origin list overridable via the
`CORS_ALLOWED_ORIGINS` env var (comma-separated). The default
when unset is the single prod origin. Browsers must NOT see the
ACAO header echoed back for any origin not on the list, even
when the request looks otherwise valid (this is what stops
cross-origin credential theft from a hostile page).

Pinned behaviors:
  - preflight from the prod origin returns the origin in ACAO
  - preflight from a random other origin produces NO ACAO at
    all (stricter than "ACAO != evil"; ensures the middleware
    isn't echoing wildcards or some other surprise)
  - allow_credentials=True surfaces as ACAC: true on the
    preflight (cookies / Bearer tokens require this)
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import app

ALLOWED_ORIGIN = "https://app.lap-coach.racing"
DISALLOWED_ORIGIN = "https://evil.example"


def _preflight(origin: str) -> httpx.Response:
    return TestClient(app).options(
        "/healthz",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "GET",
        },
    )


def test_cors_preflight_allows_configured_origin() -> None:
    response = _preflight(ALLOWED_ORIGIN)
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN


def test_cors_preflight_rejects_other_origin() -> None:
    """Stronger than '!= evil': the middleware must NOT emit an
    ACAO header at all for a disallowed origin. An ACAO of `*`
    or an empty string would leak permissive CORS behavior."""
    response = _preflight(DISALLOWED_ORIGIN)
    assert "access-control-allow-origin" not in response.headers


def test_cors_preflight_allows_credentials_for_configured_origin() -> None:
    """allow_credentials=True is required for the frontend to
    send the Authorization header alongside cross-origin
    requests. Pin it so accidental disabling shows up here."""
    response = _preflight(ALLOWED_ORIGIN)
    assert (
        response.headers.get("access-control-allow-credentials")
        == "true"
    )


def test_cors_origin_list_is_env_overridable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T184: the allowed-origin list reads from the
    CORS_ALLOWED_ORIGINS env var (comma-separated). With the env
    var set to a custom value, the parser must yield exactly
    that list — not the hardcoded prod default."""
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        "https://staging.lap-coach.racing,https://canary.lap-coach.racing",
    )
    from app.main import _parse_cors_origins  # local import — env-sensitive

    origins = _parse_cors_origins()
    assert origins == [
        "https://staging.lap-coach.racing",
        "https://canary.lap-coach.racing",
    ]


def test_cors_origin_list_default_is_prod_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When CORS_ALLOWED_ORIGINS is unset, the parser must yield
    exactly the single prod origin — not the empty list (which
    would unintentionally lock out everyone) and not a wildcard."""
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    from app.main import _parse_cors_origins

    origins = _parse_cors_origins()
    assert origins == ["https://app.lap-coach.racing"]
