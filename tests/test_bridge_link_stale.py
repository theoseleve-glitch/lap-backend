"""T029 (sprint2-backend) — bridge link stale lifecycle.

When a Session referenced in a TransferLink's `session_pair` is
deleted, the link should be marked stale (stale_at = now()) so
consumers can either filter it out or render a "session no longer
available" badge.

The trigger point is a domain helper — wherever someone deletes
a Session, they call `mark_transfer_links_stale_for_session(...)`.
A future session-delete endpoint will wire it in; for sprint
scope T029 ships the mechanism + tests.

Properties pinned:
- Marks links containing the session_id as stale.
- Doesn't touch links that don't reference it.
- Idempotent: a link that is already stale keeps its original
  stale_at (we don't overwrite — the first deletion is the truth).
- Returns the count of links newly marked, so callers can log.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from domain.bridge import mark_transfer_links_stale_for_session
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, TransferLink, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncGenerator[Any, None]:
    """A bare DB-only fixture — no FastAPI app. T029 lives in the
    domain layer, so we don't need the full configured_app."""
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    return make_session_factory(engine)


async def _seed_driver_with_link(
    factory: Any,
    *,
    session_pair: list[Any],
    stale_at: datetime | None = None,
) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    link_id = uuid6.uuid7()
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=f"u_{user_id}",
                email=f"{user_id}@t",
            )
        )
        db.add(
            DriverProfile(id=driver_id, user_id=user_id, display_name="A")
        )
        db.add(
            TransferLink(
                id=link_id,
                driver_id=driver_id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                created_by="auto",
                session_pair=[str(s) for s in session_pair],
                link_reason="same_track",
                shape_match_score=0.7,
                intensity_match_score=0.6,
                stale_at=stale_at,
            )
        )
        await db.commit()
    return {"driver_id": driver_id, "link_id": link_id}


async def _read_link(factory: Any, link_id: Any) -> TransferLink:
    async with factory() as db:
        return (
            await db.execute(
                select(TransferLink).where(TransferLink.id == link_id)
            )
        ).scalar_one()


# -- behavior ------------------------------------------------------


def test_marks_links_containing_the_session_as_stale(
    factory: Any,
) -> None:
    session_a = uuid6.uuid7()
    session_b = uuid6.uuid7()
    seeded = asyncio.run(
        _seed_driver_with_link(factory, session_pair=[session_a, session_b])
    )

    async def _run() -> int:
        async with factory() as db:
            count = await mark_transfer_links_stale_for_session(
                db, session_a
            )
            await db.commit()
            return count

    n = asyncio.run(_run())
    assert n == 1
    link = asyncio.run(_read_link(factory, seeded["link_id"]))
    assert link.stale_at is not None


def test_does_not_mark_links_that_dont_reference_the_session(
    factory: Any,
) -> None:
    """A link whose session_pair doesn't contain the deleted
    session_id stays not-stale. This is the property that makes
    the helper safe to call from any session-delete path — it
    won't false-positive on neighboring drivers' links."""
    session_a = uuid6.uuid7()
    session_b = uuid6.uuid7()
    unrelated = uuid6.uuid7()
    seeded = asyncio.run(
        _seed_driver_with_link(factory, session_pair=[session_a, session_b])
    )

    async def _run() -> int:
        async with factory() as db:
            count = await mark_transfer_links_stale_for_session(
                db, unrelated
            )
            await db.commit()
            return count

    n = asyncio.run(_run())
    assert n == 0
    link = asyncio.run(_read_link(factory, seeded["link_id"]))
    assert link.stale_at is None


def test_idempotent_does_not_overwrite_existing_stale_at(
    factory: Any,
) -> None:
    """A link that's already stale keeps its original stale_at —
    the first deletion is the truth. Re-running on the same
    session is a no-op (count returned reflects newly-marked, not
    total stale).

    Why: if we re-stamp on every call, we lose the original
    "when did this go stale?" signal that downstream views need
    for "stale within last 7 days" filters."""
    session_a = uuid6.uuid7()
    session_b = uuid6.uuid7()
    original_stale_at = datetime(2026, 4, 1, 12, tzinfo=UTC)
    seeded = asyncio.run(
        _seed_driver_with_link(
            factory,
            session_pair=[session_a, session_b],
            stale_at=original_stale_at,
        )
    )
    # Read once before to capture the post-roundtrip canonical
    # value (SQLite strips tzinfo from DateTime, so the stored
    # value isn't byte-equal to what we passed in — that's a
    # fixture artifact, not a real bug).
    pre = asyncio.run(_read_link(factory, seeded["link_id"]))
    pre_stale_at = pre.stale_at

    async def _run() -> int:
        async with factory() as db:
            count = await mark_transfer_links_stale_for_session(
                db, session_a
            )
            await db.commit()
            return count

    n = asyncio.run(_run())
    assert n == 0
    post = asyncio.run(_read_link(factory, seeded["link_id"]))
    assert post.stale_at == pre_stale_at


def test_returns_count_of_newly_marked_links(
    factory: Any,
) -> None:
    """Two links share session_a; both should be newly marked
    stale, count returned should be 2."""
    session_a = uuid6.uuid7()
    session_b = uuid6.uuid7()
    session_c = uuid6.uuid7()
    asyncio.run(
        _seed_driver_with_link(factory, session_pair=[session_a, session_b])
    )
    asyncio.run(
        _seed_driver_with_link(factory, session_pair=[session_a, session_c])
    )

    async def _run() -> int:
        async with factory() as db:
            count = await mark_transfer_links_stale_for_session(
                db, session_a
            )
            await db.commit()
            return count

    assert asyncio.run(_run()) == 2
