"""T206 — false-positive flag aggregation.

`compute_high_false_positive_rules` aggregates the
`coaching_insights.flagged_false_positive` writes from T205 into
per-rule stats. Returns the rules whose false-positive rate
exceeds a threshold (default 0.10 per the sprint spec).

Sprint task only asks for the >10% threshold — no min-sample
guard. We include `total` and `flagged_count` in the result so
the operator sees the sample size and can decide whether a 1/1
spike is real or noise.

Sorted high-to-low by rate so the worst offenders are first.
"""

from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from infra.coaching_flag_aggregation import (
    compute_high_false_positive_rules,
)
from infra.db import make_engine, make_session_factory
from infra.orm import CoachingInsight, DriverProfile, Session, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    """Sync fixture that runs alembic upgrade and yields a
    session factory. Tests open async sessions inline via
    `async with factory() as db:` — same style as
    test_admin_kpis_endpoint, the closest precedent in this repo."""
    db_path = tmp_path / "agg.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


async def _seed_insights(
    db: AsyncSession,
    *,
    rule_specs: dict[str, tuple[int, int]],
) -> None:
    """rule_specs: {rule_id: (total_insights, flagged_count)}.
    Builds one user/driver/session and N insights per rule."""
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    session_id = uuid6.uuid7()
    now = datetime.now(UTC)
    db.add(
        User(id=user_id, clerk_user_id="agg_user", email="agg@test")
    )
    db.add(
        DriverProfile(id=driver_id, user_id=user_id, display_name="A")
    )
    db.add(
        Session(
            id=session_id, driver_id=driver_id,
            manifest={}, status="ready", created_at=now,
        )
    )
    for rule_id, (total, flagged) in rule_specs.items():
        for i in range(total):
            db.add(
                CoachingInsight(
                    id=uuid6.uuid7(), session_id=session_id,
                    driver_id=driver_id, created_at=now,
                    rule_id=rule_id, rule_version="1",
                    observation="x", evidence=[], severity="low",
                    confidence=0.5,
                    flagged_false_positive=(i < flagged),
                )
            )
    await db.commit()


async def test_empty_database_returns_empty_list(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as db:
        result = await compute_high_false_positive_rules(db)
    assert result == []


async def test_rule_with_zero_flags_is_not_returned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A clean rule (10 insights, 0 flags) has rate 0% → below
    10% threshold → not in the result."""
    async with session_factory() as db:
        await _seed_insights(db, rule_specs={"br.clean": (10, 0)})
        result = await compute_high_false_positive_rules(db)
    assert result == []


async def test_rule_at_exactly_threshold_is_not_returned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Threshold is strict `>`, not `>=`. 1/10 = 10% must NOT
    appear; 2/10 = 20% must. Pinning the boundary so a future
    refactor doesn't quietly flip it to `>=`."""
    async with session_factory() as db:
        await _seed_insights(
            db, rule_specs={"br.borderline": (10, 1)}
        )
        result = await compute_high_false_positive_rules(db)
    assert result == []


async def test_rule_above_threshold_is_returned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """11% > 10% → the rule is flagged."""
    async with session_factory() as db:
        await _seed_insights(
            db, rule_specs={"br.misfires": (100, 11)}
        )
        result = await compute_high_false_positive_rules(db)
    assert len(result) == 1
    stat = result[0]
    assert stat.rule_id == "br.misfires"
    assert stat.total == 100
    assert stat.flagged_count == 11
    assert stat.false_positive_rate == pytest.approx(0.11)


async def test_only_above_threshold_rules_returned(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mixed bag: one good rule, one borderline, one bad. Only
    the bad one shows up."""
    async with session_factory() as db:
        await _seed_insights(
            db,
            rule_specs={
                "br.good": (20, 0),        # 0%
                "br.borderline": (10, 1),  # 10% (boundary)
                "br.bad": (10, 3),         # 30%
            },
        )
        result = await compute_high_false_positive_rules(db)
    fired_ids = {s.rule_id for s in result}
    assert fired_ids == {"br.bad"}


async def test_results_sorted_by_rate_descending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Worst offenders first so the operator's eye lands there."""
    async with session_factory() as db:
        await _seed_insights(
            db,
            rule_specs={
                "br.bad15": (100, 15),  # 15%
                "br.bad50": (10, 5),    # 50%
                "br.bad22": (50, 11),   # 22%
            },
        )
        result = await compute_high_false_positive_rules(db)
    rates = [s.false_positive_rate for s in result]
    assert rates == sorted(rates, reverse=True)
    assert [s.rule_id for s in result] == [
        "br.bad50", "br.bad22", "br.bad15",
    ]


async def test_threshold_argument_overrides_default(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A 5% threshold catches rules the default 10% misses."""
    async with session_factory() as db:
        await _seed_insights(
            db,
            rule_specs={
                "br.eight": (100, 8),  # 8% — between 5% and 10%
            },
        )
        default = await compute_high_false_positive_rules(db)
        custom = await compute_high_false_positive_rules(
            db, threshold=0.05
        )
    assert default == []  # 8% ≤ 10% default
    assert len(custom) == 1
    assert custom[0].rule_id == "br.eight"
