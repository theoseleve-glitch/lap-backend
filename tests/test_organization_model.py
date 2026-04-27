"""T161 — `Organization` entity + migration.

The Organization entity is the org-scoped container for team /
fleet / coaching-academy use cases. It owns:
  - `name` — display name
  - `plan` — billing plan (free / team / pro)
  - `admin_user_id` — FK → users.id, the org admin (creator)
  - `seats_limit` — int cap on member count enforced by upstream
    invitation logic (T164)

UUIDv7 ids and UTC RFC3339 timestamps per project convention.
"""

from datetime import datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

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


async def test_organization_row_roundtrips_after_migration(
    upgraded_db_url: str,
) -> None:
    """All five core fields persist and load correctly. Pin: a
    migration that drops or renames any of these columns will
    fail this test, preventing silent shape drift between the
    model definition and the on-disk schema."""
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            admin = User(
                id=uuid6.uuid7(),
                clerk_user_id="admin_clerk_id",
                email="admin@org.test",
            )
            session.add(admin)
            await session.flush()
            org = Organization(
                id=uuid6.uuid7(),
                name="Apex Racing Team",
                plan="team",
                admin_user_id=admin.id,
                seats_limit=10,
            )
            session.add(org)
            await session.commit()

            result = await session.execute(select(Organization))
            loaded = result.scalar_one()
            assert loaded.name == "Apex Racing Team"
            assert loaded.plan == "team"
            assert loaded.admin_user_id == admin.id
            assert loaded.seats_limit == 10
            assert isinstance(loaded.created_at, datetime)
    finally:
        await engine.dispose()


async def test_organization_admin_user_fk_enforced(
    upgraded_db_url: str,
) -> None:
    """Inserting an organization with an admin_user_id that
    doesn't exist must fail. Pin: the admin_user_id column is a
    real FK to users.id, not just a typed UUID column. A missing
    constraint would let orphaned organizations exist whose
    admin user was never seeded — breaks RBAC and invite-
    permission checks downstream."""
    engine = make_engine(upgraded_db_url)
    try:
        # SQLite: foreign keys must be explicitly enabled per
        # connection. The async engine's connect-event handler
        # enables them; without that, this test is a tautology.
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def enable_fk(dbapi_conn, _) -> None:  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            session.add(
                Organization(
                    id=uuid6.uuid7(),
                    name="Ghost Org",
                    plan="free",
                    admin_user_id=uuid6.uuid7(),  # never seeded
                    seats_limit=5,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


async def test_organization_seats_limit_default_does_not_apply(
    upgraded_db_url: str,
) -> None:
    """Pin: seats_limit is required (no default). Forgetting to
    set it must surface as an integrity error rather than
    silently picking 0 or NULL — the seats cap is load-bearing
    for team-plan billing and missing it is a data-quality
    issue worth catching at the boundary."""
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as session:
            admin = User(
                id=uuid6.uuid7(),
                clerk_user_id="adm2",
                email="adm2@org.test",
            )
            session.add(admin)
            await session.flush()
            session.add(
                Organization(
                    id=uuid6.uuid7(),
                    name="No Seats Org",
                    plan="free",
                    admin_user_id=admin.id,
                    # seats_limit deliberately omitted
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()
