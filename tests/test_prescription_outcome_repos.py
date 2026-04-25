from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User
from infra.outcome_repo import OutcomeRepo
from infra.prescription_repo import PrescriptionRepo

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"

_CTX = {
    "car_id": str(uuid6.uuid7()),
    "track_id": str(uuid6.uuid7()),
    "track_layout": "gp",
}
_COND = {
    "metric": "sector_2_time_ms",
    "target_value": 27500.0,
    "comparison": "lte",
    "measurement_window": "next_3_sessions",
}


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


# ----- PrescriptionRepo -----


async def test_prescription_create_persists_minimal_row(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            repo = PrescriptionRepo(db)
            pres = await repo.create(
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, 10, 0, tzinfo=UTC),
                priority="p1",
                title="Trail brake T3",
                rationale="r",
                expected_context=_CTX,
                success_condition=_COND,
                status="proposed",
            )
            assert pres.id is not None
            assert pres.driver_id == driver.id
            assert pres.priority == "p1"
            assert pres.status == "proposed"
            assert pres.weakness_episode_id is None
    finally:
        await engine.dispose()


async def test_prescription_get_returns_row(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            repo = PrescriptionRepo(db)
            created = await repo.create(
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                priority="p2",
                title="t",
                rationale="r",
                expected_context=_CTX,
                success_condition=_COND,
                status="proposed",
            )
            loaded = await repo.get(created.id)
            assert loaded is not None
            assert loaded.id == created.id
    finally:
        await engine.dispose()


async def test_prescription_get_returns_none_for_missing(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            repo = PrescriptionRepo(db)
            assert await repo.get(uuid6.uuid7()) is None
    finally:
        await engine.dispose()


async def test_prescription_list_by_driver_filters_by_driver(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            d1 = await _seed_driver(db)
            user2 = User(id=uuid6.uuid7(), clerk_user_id="u2", email="u2@test")
            d2 = DriverProfile(
                id=uuid6.uuid7(), user_id=user2.id, display_name="B"
            )
            db.add_all([user2, d2])
            await db.commit()
            repo = PrescriptionRepo(db)
            for n in (1, 2):
                await repo.create(
                    driver_id=d1.id,
                    created_at=datetime(2026, 4, 25, tzinfo=UTC),
                    priority="p1",
                    title=f"d1-{n}",
                    rationale="r",
                    expected_context=_CTX,
                    success_condition=_COND,
                    status="proposed",
                )
            await repo.create(
                driver_id=d2.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                priority="p1",
                title="d2-1",
                rationale="r",
                expected_context=_CTX,
                success_condition=_COND,
                status="proposed",
            )
            d1_pres = await repo.list_by_driver(d1.id)
            assert len(d1_pres) == 2
            d2_pres = await repo.list_by_driver(d2.id)
            assert len(d2_pres) == 1
            assert d2_pres[0].title == "d2-1"
    finally:
        await engine.dispose()


async def test_prescription_update_status_changes_status(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            repo = PrescriptionRepo(db)
            created = await repo.create(
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                priority="p1",
                title="t",
                rationale="r",
                expected_context=_CTX,
                success_condition=_COND,
                status="proposed",
            )
            updated = await repo.update_status(created.id, "accepted")
            assert updated is not None
            assert updated.status == "accepted"
    finally:
        await engine.dispose()


async def test_prescription_update_status_returns_none_for_missing(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            repo = PrescriptionRepo(db)
            assert await repo.update_status(uuid6.uuid7(), "accepted") is None
    finally:
        await engine.dispose()


# ----- OutcomeRepo -----


async def test_outcome_create_persists_row(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            pres_repo = PrescriptionRepo(db)
            pres = await pres_repo.create(
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                priority="p1",
                title="t",
                rationale="r",
                expected_context=_CTX,
                success_condition=_COND,
                status="accepted",
            )
            outcome_repo = OutcomeRepo(db)
            outcome = await outcome_repo.create(
                prescription_id=pres.id,
                measured_at=datetime(2026, 4, 27, tzinfo=UTC),
                success_condition_met=True,
                measured_value=27_200.0,
                delta_vs_baseline=-300.0,
                confidence_change=0.1,
                prescription_efficacy_score=0.8,
            )
            assert outcome.id is not None
            assert outcome.prescription_id == pres.id
            assert outcome.success_condition_met is True
    finally:
        await engine.dispose()


async def test_outcome_get_returns_row(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            pres_repo = PrescriptionRepo(db)
            pres = await pres_repo.create(
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                priority="p1",
                title="t",
                rationale="r",
                expected_context=_CTX,
                success_condition=_COND,
                status="accepted",
            )
            outcome_repo = OutcomeRepo(db)
            created = await outcome_repo.create(
                prescription_id=pres.id,
                measured_at=datetime(2026, 4, 27, tzinfo=UTC),
                success_condition_met=False,
                measured_value=28_500.0,
                delta_vs_baseline=200.0,
                confidence_change=-0.05,
                prescription_efficacy_score=0.3,
            )
            loaded = await outcome_repo.get(created.id)
            assert loaded is not None
            assert loaded.id == created.id
    finally:
        await engine.dispose()


async def test_outcome_get_for_prescription_finds_linked_outcome(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            pres_repo = PrescriptionRepo(db)
            pres = await pres_repo.create(
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                priority="p1",
                title="t",
                rationale="r",
                expected_context=_CTX,
                success_condition=_COND,
                status="accepted",
            )
            outcome_repo = OutcomeRepo(db)
            created = await outcome_repo.create(
                prescription_id=pres.id,
                measured_at=datetime(2026, 4, 27, tzinfo=UTC),
                success_condition_met=True,
                measured_value=27_300.0,
                delta_vs_baseline=-200.0,
                confidence_change=0.1,
                prescription_efficacy_score=0.7,
            )
            found = await outcome_repo.get_for_prescription(pres.id)
            assert found is not None
            assert found.id == created.id
    finally:
        await engine.dispose()


async def test_outcome_get_for_prescription_returns_none_when_unlinked(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            pres_repo = PrescriptionRepo(db)
            pres = await pres_repo.create(
                driver_id=driver.id,
                created_at=datetime(2026, 4, 25, tzinfo=UTC),
                priority="p1",
                title="t",
                rationale="r",
                expected_context=_CTX,
                success_condition=_COND,
                status="proposed",
            )
            outcome_repo = OutcomeRepo(db)
            assert await outcome_repo.get_for_prescription(pres.id) is None
    finally:
        await engine.dispose()
