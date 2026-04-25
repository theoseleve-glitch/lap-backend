from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CoachingInsight,
    DriverProfile,
    Session,
    User,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def _seed_session(db) -> tuple[Session, DriverProfile]:  # type: ignore[no-untyped-def]
    user = User(id=uuid6.uuid7(), clerk_user_id="u", email="u@test")
    driver = DriverProfile(id=uuid6.uuid7(), user_id=user.id, display_name="A")
    session = Session(id=uuid6.uuid7(), driver_id=driver.id, manifest={})
    db.add_all([user, driver, session])
    await db.commit()
    return session, driver


async def test_coaching_insight_roundtrips_all_fields(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            sess, driver = await _seed_session(db)
            created = datetime(2026, 4, 25, 12, 0, tzinfo=UTC)
            evidence = [
                "lap 5 sector 2: -0.4s under target",
                "throttle channel: 17% earlier release at T3 exit",
            ]
            insight = CoachingInsight(
                id=uuid6.uuid7(),
                session_id=sess.id,
                driver_id=driver.id,
                created_at=created,
                rule_id="rule.technique.trail_braking",
                rule_version="v1.2",
                observation=(
                    "Driver consistently lifts off the brake too early at T3 entry."
                ),
                evidence=evidence,
                likely_cause=(
                    "Anticipating mid-corner understeer based on prior session."
                ),
                severity="medium",
                confidence=0.78,
                corner_tag="T3",
                suggested_prescription_id=None,
                flagged_false_positive=False,
            )
            db.add(insight)
            await db.commit()

            loaded = (await db.execute(select(CoachingInsight))).scalar_one()
            assert loaded.session_id == sess.id
            assert loaded.driver_id == driver.id
            assert loaded.created_at == created
            assert loaded.rule_id == "rule.technique.trail_braking"
            assert loaded.rule_version == "v1.2"
            assert loaded.observation.startswith("Driver consistently")
            assert loaded.evidence == evidence
            assert loaded.likely_cause is not None
            assert "Anticipating" in loaded.likely_cause
            assert loaded.severity == "medium"
            assert loaded.confidence == 0.78
            assert loaded.corner_tag == "T3"
            assert loaded.suggested_prescription_id is None
            assert loaded.flagged_false_positive is False
    finally:
        await engine.dispose()


async def test_coaching_insight_applies_pydantic_defaults(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            sess, driver = await _seed_session(db)
            insight = CoachingInsight(
                id=uuid6.uuid7(),
                session_id=sess.id,
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                rule_id="rule.x",
                rule_version="v1",
                observation="Minimal observation.",
                severity="low",
                confidence=0.5,
            )
            db.add(insight)
            await db.commit()
            loaded = (await db.execute(select(CoachingInsight))).scalar_one()
            assert loaded.evidence == []
            assert loaded.likely_cause is None
            assert loaded.corner_tag is None
            assert loaded.suggested_prescription_id is None
            assert loaded.flagged_false_positive is False
    finally:
        await engine.dispose()
