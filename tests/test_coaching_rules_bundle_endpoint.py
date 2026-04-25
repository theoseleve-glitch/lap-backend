"""T204 — `GET /api/v1/coaching/rules/bundle?version=latest`.

Public endpoint that ships the compiled, signed coaching rules
bundle to clients (desktop/mobile). Wraps three earlier pieces:
T203's `compile_library` (YAML → portable JSON), T204's
`bundle.sign_bundle` (HMAC envelope), and a thin router that
gates on the signing secret being configured.

Returns the bundle directly as JSON. Auth-free — same posture as
the catalog (T140) and the rules listing (T202): rules are
non-sensitive, integrity is ensured by HMAC verification on the
client side.

`version=latest` is the only supported value today (and is also
the default when omitted). Anything else returns 400 — opens the
door for T-future to support pinned-version retrieval without
breaking the wire shape now.
"""

import hashlib
import hmac
import json
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config as AlembicConfig
from fastapi.testclient import TestClient

from alembic import command
from app.main import app

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
SIGNING_SECRET = "test-bundle-signing-secret-not-real"


@pytest.fixture
def configured_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    monkeypatch.setenv(
        "COACHING_BUNDLE_SIGNING_SECRET", SIGNING_SECRET
    )
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    # Bust the lru_cache on get_config so the new env vars are
    # reflected — get_config is cached per-process and other
    # fixtures may have populated it with no signing secret set.
    from app.deps import get_config
    get_config.cache_clear()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        get_config.cache_clear()


@pytest.fixture
def configured_app_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Variant: signing secret intentionally absent. Used by the
    503 test — server can't sign without it, so the endpoint must
    refuse rather than ship an unsigned bundle."""
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    monkeypatch.delenv("COACHING_BUNDLE_SIGNING_SECRET", raising=False)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    from app.deps import get_config
    get_config.cache_clear()
    try:
        yield
    finally:
        app.dependency_overrides.clear()
        get_config.cache_clear()


def test_bundle_unauthenticated_returns_200(
    configured_app: None,  # noqa: ARG001
) -> None:
    resp = TestClient(app).get(
        "/api/v1/coaching/rules/bundle?version=latest"
    )
    assert resp.status_code == 200


def test_bundle_omitted_version_returns_200(
    configured_app: None,  # noqa: ARG001
) -> None:
    """Spec says ?version=latest is the canonical form, but
    omitting the param defaults to latest — keeps the URL pretty
    in the common case."""
    resp = TestClient(app).get("/api/v1/coaching/rules/bundle")
    assert resp.status_code == 200


def test_bundle_unsupported_version_returns_400(
    configured_app: None,  # noqa: ARG001
) -> None:
    resp = TestClient(app).get(
        "/api/v1/coaching/rules/bundle?version=v0.deadbeef"
    )
    assert resp.status_code == 400


def test_bundle_response_carries_format_version_and_rules(
    configured_app: None,  # noqa: ARG001
) -> None:
    resp = TestClient(app).get("/api/v1/coaching/rules/bundle")
    body = resp.json()
    assert body["format_version"] == 1
    assert isinstance(body["rules"], list)
    assert len(body["rules"]) >= 10  # 10 braking rules already shipped


def test_bundle_response_carries_bundle_version_hash(
    configured_app: None,  # noqa: ARG001
) -> None:
    """`bundle_version` must be a 64-char SHA-256 hex — clients
    diff by string equality and skip refetch when unchanged."""
    resp = TestClient(app).get("/api/v1/coaching/rules/bundle")
    body = resp.json()
    bv = body["bundle_version"]
    assert isinstance(bv, str) and len(bv) == 64
    int(bv, 16)  # raises if not hex


def test_bundle_response_carries_signature_block(
    configured_app: None,  # noqa: ARG001
) -> None:
    resp = TestClient(app).get("/api/v1/coaching/rules/bundle")
    body = resp.json()
    sig = body["signature"]
    assert sig["alg"] == "HMAC-SHA256"
    assert isinstance(sig["value"], str) and len(sig["value"]) == 64


def test_bundle_signature_verifies_against_canonical_body(
    configured_app: None,  # noqa: ARG001
) -> None:
    """End-to-end HMAC contract: a client with the shared secret
    can canonical-JSON the body (sans signature), HMAC-SHA256 it,
    and compare to signature.value. If this assertion fails, no
    client implementation will be able to verify the bundle."""
    resp = TestClient(app).get("/api/v1/coaching/rules/bundle")
    body = resp.json()
    sig_block = body.pop("signature")
    canonical: bytes = json.dumps(
        body, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    expected = hmac.new(
        SIGNING_SECRET.encode("utf-8"),
        canonical,
        hashlib.sha256,
    ).hexdigest()
    assert sig_block["value"] == expected


def test_bundle_repeated_calls_return_same_version(
    configured_app: None,  # noqa: ARG001
) -> None:
    """Content-addressed: if the YAML hasn't changed, two
    consecutive requests yield the same bundle_version. Clients
    rely on this to short-circuit refetches."""
    client = TestClient(app)
    a = client.get("/api/v1/coaching/rules/bundle").json()
    b = client.get("/api/v1/coaching/rules/bundle").json()
    assert a["bundle_version"] == b["bundle_version"]
    assert a["signature"]["value"] == b["signature"]["value"]


def test_bundle_signing_secret_not_configured_returns_503(
    configured_app_no_secret: None,  # noqa: ARG001
) -> None:
    """Refusing to ship an unsigned bundle is the correct posture
    — easier to debug a 503 than to discover later that production
    has been serving unsigned payloads. Mirrors the Stripe-webhook
    `stripe_webhook_secret_not_configured` 503."""
    resp = TestClient(app).get("/api/v1/coaching/rules/bundle")
    assert resp.status_code == 503
    assert resp.json()["detail"] == (
        "coaching_bundle_signing_secret_not_configured"
    )


def _bundle_top_level_keys() -> set[str]:
    """Wire-format invariant: only these four top-level keys.
    Clients can validate the response against this allow-list."""
    return {"format_version", "rules", "bundle_version", "signature"}


def test_bundle_response_has_only_documented_top_level_keys(
    configured_app: None,  # noqa: ARG001
) -> None:
    resp = TestClient(app).get("/api/v1/coaching/rules/bundle")
    body = resp.json()
    assert set(body.keys()) == _bundle_top_level_keys()


def _signature_keys() -> set[str]:
    return {"alg", "value"}


def test_bundle_signature_block_has_only_alg_and_value(
    configured_app: None,  # noqa: ARG001
) -> None:
    resp = TestClient(app).get("/api/v1/coaching/rules/bundle")
    body = resp.json()
    assert set(body["signature"].keys()) == _signature_keys()


def _no_unused_imports() -> dict[str, Any]:
    """Sentinel for ruff F401 — these are referenced via tests
    that pop & rebuild canonical JSON. Keeping the helper near
    the imports for clarity."""
    return {}
