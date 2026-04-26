"""T237 — `verbose_me_read` flag gates an extra log line on /me.

This is the "used on one endpoint" half of T237: the flag module
exists and the GET /api/v1/me handler reads it, demonstrating
the gradual-rollout pattern end-to-end.

The flag controls a *log line*, not a response field — a flag
should never change the API contract. Logs are an internal
observability concern; gating extra detail behind a flag means
we can crank up debug visibility for one environment without
shipping new code.
"""

from __future__ import annotations

import time
from collections.abc import Generator
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient

from app.main import app

# Reuse the rich /me test fixture set so we don't duplicate the
# JWKS / DB / Clerk handshake plumbing. F401 silences "unused";
# F811 silences "redefinition" caused by pytest fixture params
# shadowing the import to ruff's eye.
from tests.test_me_endpoint import _sign, configured_app  # noqa: F401


def _bearer(keys: dict[str, Any]) -> str:
    now = int(time.time())
    return _sign(
        keys["priv_pem"],
        {"sub": "user_flag_test", "email": "flag@test", "exp": now + 3600},
        keys["kid"],
    )


@pytest.fixture
def captured_logs() -> Generator[list[dict[str, Any]], None, None]:
    """Capture structlog events emitted during the wrapped block."""
    with structlog.testing.capture_logs() as logs:
        yield logs


def test_flag_off_emits_no_verbose_me_log(
    configured_app: dict[str, Any],  # noqa: F811
    captured_logs: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FLAG_VERBOSE_ME_READ", raising=False)
    client = TestClient(app)
    token = _bearer(configured_app["keys"])

    resp = client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    # The verbose log line must NOT appear when the flag is off —
    # default behavior preserves the silent baseline.
    me_read_events = [
        log for log in captured_logs if log.get("event") == "me.read.verbose"
    ]
    assert me_read_events == []


def test_flag_on_emits_verbose_me_log(
    configured_app: dict[str, Any],  # noqa: F811
    captured_logs: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FLAG_VERBOSE_ME_READ", "1")
    client = TestClient(app)
    token = _bearer(configured_app["keys"])

    resp = client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    )

    assert resp.status_code == 200
    me_read_events = [
        log for log in captured_logs if log.get("event") == "me.read.verbose"
    ]
    assert len(me_read_events) == 1
    # The verbose payload should include the user identifier so a
    # rolled-out variant can be traced back to who triggered it.
    event = me_read_events[0]
    assert event.get("clerk_user_id") == "user_flag_test"


def test_response_shape_unchanged_by_flag(
    configured_app: dict[str, Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Critical contract: the API response is identical regardless
    of flag state. A flag must never mutate the wire format —
    that turns "gradual rollout" into "client-visible breakage"."""
    client = TestClient(app)
    token = _bearer(configured_app["keys"])

    monkeypatch.delenv("FLAG_VERBOSE_ME_READ", raising=False)
    body_off = client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    ).json()

    monkeypatch.setenv("FLAG_VERBOSE_ME_READ", "1")
    body_on = client.get(
        "/api/v1/me", headers={"Authorization": f"Bearer {token}"}
    ).json()

    assert body_off == body_on
