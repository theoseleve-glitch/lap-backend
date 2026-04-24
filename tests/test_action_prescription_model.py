from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionPrescription,
    DriverProfile,
    Session,
    User,
    WeaknessEpisode,
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


async def _seed_driver(db) -> DriverProfile:  # type: ignore[no-untyped-def]
    user = User(id=uuid6.uuid7(), clerk_user_id="u", email="u@test")
    driver = DriverProfile(id=uuid6.uuid7(), user_id=user.id, display_name="A")
    db.add_all([user, driver])
    await db.commit()
    return driver


_CTX = {
    "car_id": str(uuid6.uuid7()),
    "track_id": str(uuid6.uuid7()),
    "track_layout": "gp",
    "environment_tag": "sim_practice",
    "conditions": "dry",
}
_COND = {
    "metric": "sector_2_time_ms",
    "target_value": 27500.0,
    "comparison": "lte",
    "target_pct": None,
    "measurement_window": "next_3_sessions",
}


async def test_action_prescription_roundtrips_all_fields(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            ep = WeaknessEpisode(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                category="technique",
                subcategory="trail_braking",
                first_observed_at=datetime(2026, 4, 1, tzinfo=UTC),
                last_observed_at=datetime(2026, 4, 20, tzinfo=UTC),
                recurrence_count=5,
                severity="medium",
                confidence=0.7,
                status="open",
                library_rule_id="rule.x",
            )
            db.add(ep)
            await db.commit()

            created = datetime(2026, 4, 25, 10, 0, tzinfo=UTC)
            valid_until = datetime(2026, 5, 2, 10, 0, tzinfo=UTC)
            accepted = datetime(2026, 4, 25, 11, 0, tzinfo=UTC)
            outcome = uuid6.uuid7()
            pres = ActionPrescription(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                weakness_episode_id=ep.id,
                created_at=created,
                priority="p1",
                due_before_session_id=None,
                valid_until=valid_until,
                title="Trail brake into T3",
                rationale=(
                    "Consistent under-rotation at mid-entry;"
                    " progressive release will improve rotation."
                ),
                drill_template_id="drill.trail_brake.v2",
                expected_context=_CTX,
                success_condition=_COND,
                status="accepted",
                accepted_at=accepted,
                outcome_id=outcome,
            )
            db.add(pres)
            await db.commit()

            loaded = (
                await db.execute(select(ActionPrescription))
            ).scalar_one()
            assert loaded.driver_id == driver.id
            assert loaded.weakness_episode_id == ep.id
            assert loaded.created_at == created
            assert loaded.priority == "p1"
            assert loaded.due_before_session_id is None
            assert loaded.valid_until == valid_until
            assert loaded.title == "Trail brake into T3"
            assert loaded.rationale.startswith("Consistent")
            assert loaded.drill_template_id == "drill.trail_brake.v2"
            assert loaded.expected_context == _CTX
            assert loaded.success_condition == _COND
            assert loaded.status == "accepted"
            assert loaded.accepted_at == accepted
            assert loaded.outcome_id == outcome
    finally:
        await engine.dispose()


async def test_prescription_can_reference_due_before_session(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            sess = Session(id=uuid6.uuid7(), driver_id=driver.id, manifest={})
            db.add(sess)
            await db.commit()

            pres = ActionPrescription(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                priority="p0",
                due_before_session_id=sess.id,
                title="Warm up tyres",
                rationale="Cold-tyre oversteer observed.",
                expected_context=_CTX,
                success_condition=_COND,
                status="proposed",
            )
            db.add(pres)
            await db.commit()
            loaded = (
                await db.execute(select(ActionPrescription))
            ).scalar_one()
            assert loaded.due_before_session_id == sess.id
    finally:
        await engine.dispose()


async def test_prescription_without_weakness_episode_roundtrips(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            pres = ActionPrescription(
                id=uuid6.uuid7(),
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                priority="p2",
                title="General focus drill",
                rationale="Broad consistency practice.",
                expected_context=_CTX,
                success_condition=_COND,
                status="proposed",
            )
            db.add(pres)
            await db.commit()
            loaded = (
                await db.execute(select(ActionPrescription))
            ).scalar_one()
            assert loaded.weakness_episode_id is None
            assert loaded.due_before_session_id is None
            assert loaded.valid_until is None
            assert loaded.drill_template_id is None
            assert loaded.accepted_at is None
            assert loaded.outcome_id is None
    finally:
        await engine.dispose()
