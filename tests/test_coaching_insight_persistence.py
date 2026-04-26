"""T207 — bridge `run_rules` dataclass output to the
`coaching_insights` ORM table.

`coaching.library.rule_runner.run_rules` returns an in-memory
list of `CoachingInsight` dataclasses. T207 wires those into the
DB so T206's flag-rate aggregation, T208's WeaknessEpisode
opener, and admin/UX surfaces can read past insights back.

The persistence helper lives in `infra/` so the coaching library
keeps zero SQLAlchemy dependency — easier to test, easier to
re-use in other persistence layers (event log, S3 archive) later.
"""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from coaching.library.rule_runner import CoachingInsight as RunnerInsight
from infra.coaching_insight_persistence import persist_insights
from infra.db import make_engine, make_session_factory
from infra.orm import CoachingInsight, DriverProfile, Session, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    db_path = tmp_path / "persist.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


async def _seed_session(
    db: AsyncSession,
) -> tuple[uuid6.UUID, uuid6.UUID]:
    """Stand up the User+Driver+Session insights need to FK to.
    Returns (driver_id, session_id)."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_id = uuid6.uuid7()
    db.add(
        User(id=user_id, clerk_user_id="persist_u", email="p@test")
    )
    db.add(
        DriverProfile(id=driver_id, user_id=user_id, display_name="A")
    )
    db.add(
        Session(
            id=session_id, driver_id=driver_id,
            manifest={}, status="ready", created_at=datetime.now(UTC),
        )
    )
    await db.commit()
    return driver_id, session_id


def _runner_insight(
    rule_id: str = "br.late_braking.apex_speed_loss",
    *,
    version: int = 1,
    explanation: str = "You lose 12 km/h at the apex of Turn 1.",
) -> RunnerInsight:
    return RunnerInsight(
        rule_id=rule_id,
        rule_version=version,
        category="technique",
        subcategory="late_braking",
        difficulty="beginner",
        explanation=explanation,
    )


async def test_persist_empty_list_writes_nothing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """No insights → no rows. Returns an empty list of IDs."""
    async with session_factory() as db:
        driver_id, session_id = await _seed_session(db)
        ids = await persist_insights(
            db,
            insights=[],
            session_id=session_id,
            driver_id=driver_id,
            now=datetime.now(UTC),
        )
        rows = (await db.execute(select(CoachingInsight))).scalars().all()
    assert ids == []
    assert list(rows) == []


async def test_persist_single_insight_writes_one_row(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver_id, session_id = await _seed_session(db)
        ids = await persist_insights(
            db,
            insights=[_runner_insight()],
            session_id=session_id,
            driver_id=driver_id,
            now=datetime.now(UTC),
        )
        rows = (await db.execute(select(CoachingInsight))).scalars().all()
    assert len(ids) == 1
    assert len(rows) == 1
    assert rows[0].id == ids[0]


async def test_persist_links_session_and_driver(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        driver_id, session_id = await _seed_session(db)
        await persist_insights(
            db,
            insights=[_runner_insight()],
            session_id=session_id,
            driver_id=driver_id,
            now=datetime.now(UTC),
        )
        row = (
            await db.execute(select(CoachingInsight))
        ).scalar_one()
    assert row.session_id == session_id
    assert row.driver_id == driver_id


async def test_persist_stores_rule_id_and_string_version(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Dataclass carries int (arithmetic comparisons in-memory);
    ORM column is str (room for future '3.1.0-beta' without a
    migration). The bridge converts at the boundary."""
    async with session_factory() as db:
        driver_id, session_id = await _seed_session(db)
        await persist_insights(
            db,
            insights=[
                _runner_insight(rule_id="br.test_rule", version=7)
            ],
            session_id=session_id,
            driver_id=driver_id,
            now=datetime.now(UTC),
        )
        row = (
            await db.execute(select(CoachingInsight))
        ).scalar_one()
    assert row.rule_id == "br.test_rule"
    assert row.rule_version == "7"
    assert isinstance(row.rule_version, str)


async def test_persist_stores_explanation_as_observation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The dataclass `explanation` field is the user-facing
    rendered text — that maps to `observation` in the ORM (the
    canonical name for the human-readable string in
    coaching_insights)."""
    async with session_factory() as db:
        driver_id, session_id = await _seed_session(db)
        await persist_insights(
            db,
            insights=[
                _runner_insight(
                    explanation="Brake pressure varies 20% in S1."
                )
            ],
            session_id=session_id,
            driver_id=driver_id,
            now=datetime.now(UTC),
        )
        row = (
            await db.execute(select(CoachingInsight))
        ).scalar_one()
    assert row.observation == "Brake pressure varies 20% in S1."


async def test_persist_uses_provided_now_for_created_at(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Caller-supplied `now` lets a Celery task replay a run
    deterministically and emit insights with a stable timestamp."""
    fixed = datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC)
    async with session_factory() as db:
        driver_id, session_id = await _seed_session(db)
        await persist_insights(
            db,
            insights=[_runner_insight()],
            session_id=session_id,
            driver_id=driver_id,
            now=fixed,
        )
        row = (
            await db.execute(select(CoachingInsight))
        ).scalar_one()
    # SQLite drops timezone info on round-trip — compare the naive
    # parts. The behavior we care about is "the value the caller
    # passed is what landed", not tz fidelity (which is a SQLAlchemy
    # dialect concern handled elsewhere).
    assert row.created_at.replace(tzinfo=UTC) == fixed


async def test_persist_defaults_flagged_false_positive_to_false(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """T205's flag endpoint flips this to True later. On insert
    it's always False — the user hasn't seen it yet, can't have
    flagged it."""
    async with session_factory() as db:
        driver_id, session_id = await _seed_session(db)
        await persist_insights(
            db,
            insights=[_runner_insight()],
            session_id=session_id,
            driver_id=driver_id,
            now=datetime.now(UTC),
        )
        row = (
            await db.execute(select(CoachingInsight))
        ).scalar_one()
    assert row.flagged_false_positive is False


async def test_persist_multiple_insights_writes_multiple_rows(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three rules fired → three rows with distinct ids,
    preserving input order in the returned id list (so callers
    can correlate index-wise if they want to)."""
    insights = [
        _runner_insight(rule_id="br.a", explanation="msg a"),
        _runner_insight(rule_id="br.b", explanation="msg b"),
        _runner_insight(rule_id="br.c", explanation="msg c"),
    ]
    async with session_factory() as db:
        driver_id, session_id = await _seed_session(db)
        ids = await persist_insights(
            db,
            insights=insights,
            session_id=session_id,
            driver_id=driver_id,
            now=datetime.now(UTC),
        )
        rows = (
            await db.execute(
                select(CoachingInsight).order_by(CoachingInsight.rule_id)
            )
        ).scalars().all()
    assert len(ids) == 3
    assert len(set(ids)) == 3  # all distinct
    assert [r.rule_id for r in rows] == ["br.a", "br.b", "br.c"]


async def test_persist_assigns_uuid7_ids(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Per project convention (CLAUDE.md), all IDs are UUID7 —
    monotonically time-sortable. Persisting two batches in order
    should produce ids that compare in that order."""
    async with session_factory() as db:
        driver_id, session_id = await _seed_session(db)
        ids_first = await persist_insights(
            db,
            insights=[_runner_insight(rule_id="br.first")],
            session_id=session_id,
            driver_id=driver_id,
            now=datetime.now(UTC),
        )
        ids_second = await persist_insights(
            db,
            insights=[_runner_insight(rule_id="br.second")],
            session_id=session_id,
            driver_id=driver_id,
            now=datetime.now(UTC),
        )
    assert ids_first[0] < ids_second[0]
