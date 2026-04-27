"""T162 — `OrganizationMember` entity + migration.

The OrganizationMember entity is the join table between orgs
(T161) and users with role information. It owns:
  - `org_id` — FK → organizations.id, cascade on org delete
  - `user_id` — FK → users.id, cascade on user delete (GDPR)
  - `role` — String(32), one of {admin, coach, driver}; the
    enum is enforced upstream at the endpoint layer (T163-T165),
    not via DB CHECK, to stay consistent with the codebase
    convention (e.g., User.subscription_status, role-style
    enums elsewhere).

UNIQUE(org_id, user_id) — a user can hold only one role per
org. Re-inviting an existing member with a different role goes
through the role-update path (T164/T165), not through inserting
a duplicate row.
"""

from datetime import datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import Organization, OrganizationMember, User

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


async def _seed_admin_user_and_org(
    session_factory: object,
) -> tuple[User, Organization]:
    async with session_factory() as session:  # type: ignore[attr-defined]
        admin = User(
            id=uuid6.uuid7(),
            clerk_user_id="t162_admin_clerk",
            email="t162admin@org.test",
        )
        session.add(admin)
        await session.flush()
        org = Organization(
            id=uuid6.uuid7(),
            name="T162 Org",
            plan="team",
            admin_user_id=admin.id,
            seats_limit=10,
        )
        session.add(org)
        await session.commit()
        return admin, org


async def test_organization_member_roundtrips_after_migration(
    upgraded_db_url: str,
) -> None:
    """All four core fields persist and load. Pin: a migration
    that drops or renames any of these will fail this test,
    preventing silent shape drift."""
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        admin, org = await _seed_admin_user_and_org(session_factory)
        async with session_factory() as session:
            coach = User(
                id=uuid6.uuid7(),
                clerk_user_id="t162_coach_clerk",
                email="coach@org.test",
            )
            session.add(coach)
            await session.flush()
            member = OrganizationMember(
                id=uuid6.uuid7(),
                org_id=org.id,
                user_id=coach.id,
                role="coach",
            )
            session.add(member)
            await session.commit()

            result = await session.execute(
                select(OrganizationMember)
            )
            loaded = result.scalar_one()
            assert loaded.org_id == org.id
            assert loaded.user_id == coach.id
            assert loaded.role == "coach"
            assert isinstance(loaded.created_at, datetime)
        # silence the otherwise-unused admin local
        assert admin.id is not None
    finally:
        await engine.dispose()


async def test_organization_member_unique_org_user_pair(
    upgraded_db_url: str,
) -> None:
    """Inserting a second OrganizationMember row with the same
    (org_id, user_id) must fail. Pin: without UNIQUE(org_id,
    user_id), a user could accumulate multiple role rows in one
    org and the role lookup becomes ambiguous (which role wins?).
    The unique constraint forces re-roling through an explicit
    update path."""
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        admin, org = await _seed_admin_user_and_org(session_factory)
        async with session_factory() as session:
            driver = User(
                id=uuid6.uuid7(),
                clerk_user_id="t162_drv_clerk",
                email="drv@org.test",
            )
            session.add(driver)
            await session.flush()
            session.add(
                OrganizationMember(
                    id=uuid6.uuid7(),
                    org_id=org.id,
                    user_id=driver.id,
                    role="driver",
                )
            )
            await session.commit()
        async with session_factory() as session:
            session.add(
                OrganizationMember(
                    id=uuid6.uuid7(),
                    org_id=org.id,
                    user_id=driver.id,
                    role="coach",  # different role, same pair
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
        assert admin.id is not None
    finally:
        await engine.dispose()


async def test_organization_member_cascades_on_org_delete(
    upgraded_db_url: str,
) -> None:
    """Deleting an Organization removes its OrganizationMember
    rows. Pin: members are tied to the org's lifecycle — orphaned
    member rows after an org is deleted would leak into team-
    membership queries and become a data-quality / compliance
    issue. The schema-level cascade enforces cleanup at the DB
    layer, not at the application layer."""
    engine = make_engine(upgraded_db_url)

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_fk(dbapi_conn, _) -> None:  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    try:
        session_factory = make_session_factory(engine)
        admin, org = await _seed_admin_user_and_org(session_factory)
        async with session_factory() as session:
            mate = User(
                id=uuid6.uuid7(),
                clerk_user_id="t162_mate_clerk",
                email="mate@org.test",
            )
            session.add(mate)
            await session.flush()
            session.add(
                OrganizationMember(
                    id=uuid6.uuid7(),
                    org_id=org.id,
                    user_id=mate.id,
                    role="driver",
                )
            )
            await session.commit()
        async with session_factory() as session:
            org_loaded = await session.get(Organization, org.id)
            assert org_loaded is not None
            await session.delete(org_loaded)
            await session.commit()
        async with session_factory() as session:
            result = await session.execute(
                select(OrganizationMember)
            )
            assert result.scalar_one_or_none() is None
        assert admin.id is not None
    finally:
        await engine.dispose()
