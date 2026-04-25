"""T202 — `GET /api/v1/coaching/rules`.

Public read-only listing of every rule in the coaching library.
Mirrors the catalog router (T140) — non-sensitive reference data
the web app consumes to render a "things the coach watches for"
page. Auth-free; ADR-009 says rules ship to clients via the
bundle endpoint anyway.

Returns id/version/category/subcategory/difficulty per rule —
the minimal "rule reference" shape. Full rule content (detection
condition, explanation templates, prescriptions) lives behind
T204's bundle endpoint, not behind this index. Keeps the list
response under 10KB even with 100+ rules in the library.
"""

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
EXPECTED_BRAKING_RULE_COUNT = 10  # T188-T197


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
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    try:
        yield
    finally:
        app.dependency_overrides.clear()


def test_list_rules_unauthenticated_returns_200(
    configured_app: None,  # noqa: ARG001
) -> None:
    """Unauth must work — same posture as the catalog router. If a
    future change accidentally adds an auth dep, this test will
    flip to 401 and force the conversation."""
    resp = TestClient(app).get("/api/v1/coaching/rules")
    assert resp.status_code == 200


def test_list_rules_returns_all_braking_rules(
    configured_app: None,  # noqa: ARG001
) -> None:
    """Sanity: the 10 braking rules (T188-T197) must all show up
    in the listing. If the loader silently skips a rule (e.g., a
    YAML failed schema validation and the failure was swallowed)
    this catches it."""
    resp = TestClient(app).get("/api/v1/coaching/rules")
    body = resp.json()
    fired_ids = {item["id"] for item in body["items"]}
    expected_braking = {
        "br.late_braking.apex_speed_loss",
        "br.early_braking.entry_speed_loss",
        "br.abrupt_release.load_transfer_mismatch",
        "br.inconsistent_pressure.sector_variance",
        "br.lockup.pattern_detection",
        "br.cold_brakes.first_lap_deficit",
        "br.over_braking.mid_corner",
        "br.balance_shift.rear_locking_entry",
        "br.trail_brake.release_too_early",
        "br.threshold_braking.not_reached",
    }
    assert expected_braking.issubset(fired_ids)


def test_list_rules_carries_required_summary_fields(
    configured_app: None,  # noqa: ARG001
) -> None:
    """Each entry must carry the minimum reference shape: id,
    version, category, subcategory, difficulty."""
    resp = TestClient(app).get("/api/v1/coaching/rules")
    body = resp.json()
    for item in body["items"]:
        assert isinstance(item["id"], str) and item["id"]
        assert isinstance(item["version"], int) and item["version"] >= 1
        assert item["category"] in ("technique", "setup", "strategy")
        assert isinstance(item["subcategory"], str) and item["subcategory"]
        assert item["difficulty"] in (
            "beginner", "intermediate", "advanced"
        )


def test_list_rules_total_matches_items_count(
    configured_app: None,  # noqa: ARG001
) -> None:
    """`total` is for future pagination but right now must equal
    `len(items)` because the endpoint returns the full set."""
    resp = TestClient(app).get("/api/v1/coaching/rules")
    body = resp.json()
    assert body["total"] == len(body["items"])
    assert body["total"] >= EXPECTED_BRAKING_RULE_COUNT


def test_list_rules_items_are_unique_by_id(
    configured_app: None,  # noqa: ARG001
) -> None:
    """The loader (T182) raises on duplicate ids at startup, so a
    duplicate here would mean someone bypassed the loader. Cheap
    invariant to assert at the wire layer too."""
    resp = TestClient(app).get("/api/v1/coaching/rules")
    body = resp.json()
    ids = [item["id"] for item in body["items"]]
    assert len(ids) == len(set(ids))


def test_list_rules_response_has_no_extra_fields(
    configured_app: None,  # noqa: ARG001
) -> None:
    """`extra="forbid"` on the schema — assert at the wire that
    only the agreed fields are present so a future expansion is a
    deliberate decision, not an accident."""
    resp = TestClient(app).get("/api/v1/coaching/rules")
    body = resp.json()
    allowed_item_keys = {
        "id", "version", "category", "subcategory", "difficulty",
    }
    allowed_top_keys = {"items", "total"}
    assert set(body.keys()) == allowed_top_keys
    for item in body["items"]:
        assert set(item.keys()) == allowed_item_keys


def _make_app_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    db = tmp_path / "test2.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return None
