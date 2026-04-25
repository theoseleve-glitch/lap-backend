"""T160 — Daily reconciliation: compare Stripe subscriptions to
the local DB, log any drift, return clean when nothing's wrong.

Pure function so it can be tested without spinning up Celery.
T160 also wires it as a daily Celery beat task; the test for the
beat schedule is separate (sanity-only) since beat itself is
exercised in production.
"""

from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import uuid6
from alembic.config import Config as AlembicConfig

from alembic import command
from infra.billing_reconcile import reconcile_billing
from infra.db import make_engine, make_session_factory
from infra.orm import User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def db_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Any, None, None]:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


async def _seed_user(
    factory: Any, sub: str, email: str, status: str
) -> User:
    async with factory() as session:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=sub,
            email=email,
            subscription_status=status,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


def _sub(user_id: str, status: str) -> dict[str, Any]:
    """Plain-dict shape that matches what `stripe.Subscription.list`
    yields when iterated (StripeObject behaves enough like a dict
    for our reconciliation; tests use plain dicts for simplicity)."""
    return {
        "id": f"sub_for_{user_id[:8]}",
        "status": status,
        "metadata": {"user_id": user_id},
    }


def _patched_iter(subs: list[dict[str, Any]]) -> Any:
    """Builds a fake iterator that mimics the Stripe SDK's
    `auto_paging_iter()` by yielding subs one at a time."""

    class FakeListing:
        def __init__(self, items: list[dict[str, Any]]) -> None:
            self._items = items

        def auto_paging_iter(self) -> Any:
            return iter(self._items)

    return FakeListing(subs)


async def test_reconcile_returns_zero_drift_when_db_matches_stripe(
    db_factory: Any,
) -> None:
    user = await _seed_user(
        db_factory, "user_rc_match", "match@test", status="active"
    )
    fake_listing = _patched_iter([_sub(str(user.id), "active")])

    with patch("stripe.Subscription.list", return_value=fake_listing):
        result = await reconcile_billing(db_factory)

    assert result.drift_count == 0
    assert result.checked_count == 1
    assert result.skipped_count == 0


async def test_reconcile_counts_drift_when_status_differs(
    db_factory: Any,
) -> None:
    # DB is stale ("trialing") but Stripe upgraded to "active" —
    # webhook must have been missed / replayed late / lost.
    user = await _seed_user(
        db_factory, "user_rc_drift", "drift@test", status="trialing"
    )
    fake_listing = _patched_iter([_sub(str(user.id), "active")])

    with patch("stripe.Subscription.list", return_value=fake_listing):
        result = await reconcile_billing(db_factory)

    assert result.drift_count == 1
    assert result.checked_count == 1
    assert result.skipped_count == 0


async def test_reconcile_skips_subscriptions_without_user_id_metadata(
    db_factory: Any,
) -> None:
    fake_listing = _patched_iter(
        [
            {
                "id": "sub_no_meta",
                "status": "active",
                "metadata": {},  # missing user_id
            }
        ]
    )

    with patch("stripe.Subscription.list", return_value=fake_listing):
        result = await reconcile_billing(db_factory)

    assert result.checked_count == 0
    assert result.skipped_count == 1


async def test_reconcile_skips_subscriptions_for_unknown_users(
    db_factory: Any,
) -> None:
    bogus_user_id = str(uuid6.uuid7())
    fake_listing = _patched_iter([_sub(bogus_user_id, "active")])

    with patch("stripe.Subscription.list", return_value=fake_listing):
        result = await reconcile_billing(db_factory)

    assert result.checked_count == 0
    assert result.skipped_count == 1


async def test_reconcile_handles_multiple_subscriptions_with_mixed_drift(
    db_factory: Any,
) -> None:
    matching = await _seed_user(
        db_factory, "user_rc_ok", "ok@test", status="active"
    )
    drifting = await _seed_user(
        db_factory, "user_rc_bad", "bad@test", status="active"
    )
    fake_listing = _patched_iter(
        [
            _sub(str(matching.id), "active"),
            _sub(str(drifting.id), "past_due"),
        ]
    )

    with patch("stripe.Subscription.list", return_value=fake_listing):
        result = await reconcile_billing(db_factory)

    assert result.checked_count == 2
    assert result.drift_count == 1
    assert result.skipped_count == 0
