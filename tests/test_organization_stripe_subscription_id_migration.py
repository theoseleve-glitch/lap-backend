"""T168 (sprint2-backend) — Organization.stripe_subscription_id.

Adds a nullable column to the `organizations` table that points at
the Stripe subscription billing this org's seats. NULL = free tier
(no Stripe sync); set = the org's seat count is synced to Stripe
on member add / remove.

Pinned behaviors:
  - column exists post-migration
  - column is nullable (free tier orgs don't bill)
  - read-write round-trip preserves the value
  - default is NULL (preserves the existing constructor surface)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import Organization, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def test_organizations_table_has_stripe_subscription_id_column(
    upgraded_db_url: str,
) -> None:
    """Migration adds the column. Pin: an admin running
    `alembic upgrade head` gets the new column without manual
    intervention."""
    engine = make_engine(upgraded_db_url)
    try:
        async with engine.connect() as conn:
            cols = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns(
                    "organizations"
                )
            )
        names = {c["name"] for c in cols}
        assert "stripe_subscription_id" in names
    finally:
        await engine.dispose()


async def test_stripe_subscription_id_is_nullable(
    upgraded_db_url: str,
) -> None:
    """NULL is the default for free-tier orgs (no Stripe sub).
    Pin: required-NOT-NULL would force every org to be billable,
    breaking the free tier."""
    engine = make_engine(upgraded_db_url)
    try:
        async with engine.connect() as conn:
            cols = await conn.run_sync(
                lambda sync_conn: inspect(sync_conn).get_columns(
                    "organizations"
                )
            )
        ssi = next(
            c for c in cols if c["name"] == "stripe_subscription_id"
        )
        assert ssi["nullable"] is True
    finally:
        await engine.dispose()


async def test_stripe_subscription_id_round_trip(
    upgraded_db_url: str,
) -> None:
    """Set value at insert, read it back. Pin: the column is
    actually wired through the ORM, not just a migration ghost."""
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as db:
            admin = User(
                id=uuid6.uuid7(),
                clerk_user_id="adm168_round",
                email="adm168_round@t",
            )
            db.add(admin)
            await db.flush()
            org = Organization(
                id=uuid6.uuid7(),
                name="T168 RT",
                plan="team",
                admin_user_id=admin.id,
                seats_limit=10,
                stripe_subscription_id="sub_test_round",
            )
            db.add(org)
            await db.commit()
            await db.refresh(org)
            assert org.stripe_subscription_id == "sub_test_round"
    finally:
        await engine.dispose()


async def test_stripe_subscription_id_defaults_to_null(
    upgraded_db_url: str,
) -> None:
    """An Organization created without the column has NULL —
    preserves the existing free-tier code path."""
    engine = make_engine(upgraded_db_url)
    try:
        factory = make_session_factory(engine)
        async with factory() as db:
            admin = User(
                id=uuid6.uuid7(),
                clerk_user_id="adm168_null",
                email="adm168_null@t",
            )
            db.add(admin)
            await db.flush()
            org = Organization(
                id=uuid6.uuid7(),
                name="T168 Null",
                plan="free",
                admin_user_id=admin.id,
                seats_limit=5,
            )
            db.add(org)
            await db.commit()
            await db.refresh(org)
            assert org.stripe_subscription_id is None
    finally:
        await engine.dispose()
