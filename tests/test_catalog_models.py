from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import (
    CarEquivalence,
    CarModel,
    TrackEquivalence,
    TrackLayout,
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


async def test_track_layout_roundtrips_all_fields(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            master = uuid6.uuid7()
            layout = TrackLayout(
                id=uuid6.uuid7(),
                source_provider="iracing",
                name="Spa-Francorchamps",
                layout_name="GP",
                start_finish_lat=50.4372,
                start_finish_lon=5.9714,
                start_finish_heading_deg=270.0,
                sector_checkpoints=[
                    {"distance_m": 0.0, "lat": 50.4372, "lon": 5.9714},
                    {"distance_m": 2370.0, "lat": 50.4358, "lon": 5.9710},
                ],
                corner_names=["La Source", "Eau Rouge", "Raidillon"],
                master_track_id=master,
            )
            db.add(layout)
            await db.commit()
            loaded = (await db.execute(select(TrackLayout))).scalar_one()
            assert loaded.source_provider == "iracing"
            assert loaded.name == "Spa-Francorchamps"
            assert loaded.layout_name == "GP"
            assert loaded.start_finish_lat == 50.4372
            assert loaded.start_finish_heading_deg == 270.0
            assert len(loaded.sector_checkpoints) == 2
            assert loaded.corner_names[0] == "La Source"
            assert loaded.master_track_id == master
    finally:
        await engine.dispose()


async def test_track_layout_provider_name_layout_is_unique(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            db.add(
                TrackLayout(
                    id=uuid6.uuid7(),
                    source_provider="iracing",
                    name="Monza",
                    layout_name="GP",
                    start_finish_lat=45.6156,
                    start_finish_lon=9.2811,
                    start_finish_heading_deg=180.0,
                    sector_checkpoints=[],
                    corner_names=[],
                    master_track_id=uuid6.uuid7(),
                )
            )
            await db.commit()
        async with session_factory() as db:
            db.add(
                TrackLayout(
                    id=uuid6.uuid7(),
                    source_provider="iracing",
                    name="Monza",
                    layout_name="GP",
                    start_finish_lat=45.6156,
                    start_finish_lon=9.2811,
                    start_finish_heading_deg=180.0,
                    sector_checkpoints=[],
                    corner_names=[],
                    master_track_id=uuid6.uuid7(),
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()


async def test_car_model_roundtrips_all_fields(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            master = uuid6.uuid7()
            car = CarModel(
                id=uuid6.uuid7(),
                source_provider="iracing",
                name="Porsche 911 GT3 R",
                car_class="gt3",
                master_car_id=master,
            )
            db.add(car)
            await db.commit()
            loaded = (await db.execute(select(CarModel))).scalar_one()
            assert loaded.source_provider == "iracing"
            assert loaded.name == "Porsche 911 GT3 R"
            assert loaded.car_class == "gt3"
            assert loaded.master_car_id == master
    finally:
        await engine.dispose()


async def test_car_model_provider_name_is_unique(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            db.add(
                CarModel(
                    id=uuid6.uuid7(),
                    source_provider="acc",
                    name="Mercedes AMG GT3",
                    car_class="gt3",
                    master_car_id=uuid6.uuid7(),
                )
            )
            await db.commit()
        async with session_factory() as db:
            db.add(
                CarModel(
                    id=uuid6.uuid7(),
                    source_provider="acc",
                    name="Mercedes AMG GT3",
                    car_class="gt3",
                    master_car_id=uuid6.uuid7(),
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()


async def test_track_equivalence_pair_roundtrips(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            t_a = TrackLayout(
                id=uuid6.uuid7(),
                source_provider="iracing",
                name="Spa-Francorchamps",
                layout_name="GP",
                start_finish_lat=50.4372,
                start_finish_lon=5.9714,
                start_finish_heading_deg=270.0,
                sector_checkpoints=[],
                corner_names=[],
                master_track_id=uuid6.uuid7(),
            )
            t_b = TrackLayout(
                id=uuid6.uuid7(),
                source_provider="acc",
                name="Spa-Francorchamps",
                layout_name="GP",
                start_finish_lat=50.4372,
                start_finish_lon=5.9714,
                start_finish_heading_deg=270.0,
                sector_checkpoints=[],
                corner_names=[],
                master_track_id=uuid6.uuid7(),
            )
            db.add_all([t_a, t_b])
            await db.commit()
            link = TrackEquivalence(
                id=uuid6.uuid7(),
                track_a_id=t_a.id,
                track_b_id=t_b.id,
                confidence=0.95,
            )
            db.add(link)
            await db.commit()
            loaded = (await db.execute(select(TrackEquivalence))).scalar_one()
            assert loaded.track_a_id == t_a.id
            assert loaded.track_b_id == t_b.id
            assert loaded.confidence == 0.95
            assert loaded.created_at is not None
    finally:
        await engine.dispose()


async def test_track_equivalence_pair_is_unique(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            t_a = TrackLayout(
                id=uuid6.uuid7(),
                source_provider="iracing",
                name="Imola",
                layout_name="GP",
                start_finish_lat=44.34,
                start_finish_lon=11.71,
                start_finish_heading_deg=0.0,
                sector_checkpoints=[],
                corner_names=[],
                master_track_id=uuid6.uuid7(),
            )
            t_b = TrackLayout(
                id=uuid6.uuid7(),
                source_provider="acc",
                name="Imola",
                layout_name="GP",
                start_finish_lat=44.34,
                start_finish_lon=11.71,
                start_finish_heading_deg=0.0,
                sector_checkpoints=[],
                corner_names=[],
                master_track_id=uuid6.uuid7(),
            )
            db.add_all([t_a, t_b])
            await db.commit()
            db.add(
                TrackEquivalence(
                    id=uuid6.uuid7(),
                    track_a_id=t_a.id,
                    track_b_id=t_b.id,
                    confidence=0.9,
                )
            )
            await db.commit()
        async with session_factory() as db:
            db.add(
                TrackEquivalence(
                    id=uuid6.uuid7(),
                    track_a_id=t_a.id,
                    track_b_id=t_b.id,
                    confidence=0.85,
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()


async def test_car_equivalence_pair_roundtrips(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            c_a = CarModel(
                id=uuid6.uuid7(),
                source_provider="iracing",
                name="Porsche 911 GT3 R",
                car_class="gt3",
                master_car_id=uuid6.uuid7(),
            )
            c_b = CarModel(
                id=uuid6.uuid7(),
                source_provider="acc",
                name="Porsche 911 GT3 R",
                car_class="gt3",
                master_car_id=uuid6.uuid7(),
            )
            db.add_all([c_a, c_b])
            await db.commit()
            link = CarEquivalence(
                id=uuid6.uuid7(),
                car_a_id=c_a.id,
                car_b_id=c_b.id,
                confidence=0.97,
            )
            db.add(link)
            await db.commit()
            loaded = (await db.execute(select(CarEquivalence))).scalar_one()
            assert loaded.car_a_id == c_a.id
            assert loaded.car_b_id == c_b.id
            assert loaded.confidence == 0.97
            assert loaded.created_at is not None
    finally:
        await engine.dispose()


async def test_car_equivalence_pair_is_unique(upgraded_db_url: str) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            c_a = CarModel(
                id=uuid6.uuid7(),
                source_provider="iracing",
                name="Audi R8 LMS",
                car_class="gt3",
                master_car_id=uuid6.uuid7(),
            )
            c_b = CarModel(
                id=uuid6.uuid7(),
                source_provider="acc",
                name="Audi R8 LMS",
                car_class="gt3",
                master_car_id=uuid6.uuid7(),
            )
            db.add_all([c_a, c_b])
            await db.commit()
            db.add(
                CarEquivalence(
                    id=uuid6.uuid7(),
                    car_a_id=c_a.id,
                    car_b_id=c_b.id,
                    confidence=0.93,
                )
            )
            await db.commit()
        async with session_factory() as db:
            db.add(
                CarEquivalence(
                    id=uuid6.uuid7(),
                    car_a_id=c_a.id,
                    car_b_id=c_b.id,
                    confidence=0.99,
                )
            )
            with pytest.raises(IntegrityError):
                await db.commit()
    finally:
        await engine.dispose()
