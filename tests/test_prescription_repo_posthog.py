"""T171 — `PrescriptionRepo.create` emits a `prescription_created`
PostHog event so the funnel from "weakness detected → drill
prescribed → drill outcome" is visible in analytics.

The event fires AFTER the DB commit so we never log a creation
that wasn't actually persisted. The repo's posthog_client kwarg
defaults to None, preserving backward compatibility with existing
callers (T079 tests pass only `db`).
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig

from alembic import command
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User
from infra.prescription_repo import PrescriptionRepo

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


class RecordingPosthog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def capture(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def set(self, **kwargs: Any) -> None:  # noqa: ARG002
        return None


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


async def _seed_driver(db: Any) -> DriverProfile:
    user = User(id=uuid6.uuid7(), clerk_user_id="u_t171", email="t171@t")
    driver = DriverProfile(
        id=uuid6.uuid7(), user_id=user.id, display_name="A"
    )
    db.add_all([user, driver])
    await db.commit()
    return driver


def _create_kwargs(driver_id: Any) -> dict[str, Any]:
    return {
        "driver_id": driver_id,
        "created_at": datetime(2026, 4, 25, 12, 0, tzinfo=UTC),
        "priority": "high",
        "title": "Late braking T1",
        "rationale": "apex speed loss > 3 km/h",
        "expected_context": {"track_id": str(uuid6.uuid7())},
        "success_condition": {"metric": "apex_speed", "delta_min": 2.0},
        "status": "open",
        "drill_template_id": "br.late_braking.v1",
        "weakness_episode_id": uuid6.uuid7(),
    }


async def test_create_emits_prescription_created_event(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            posthog = RecordingPosthog()
            repo = PrescriptionRepo(db, posthog_client=posthog)
            pres = await repo.create(**_create_kwargs(driver.id))

            assert len(posthog.events) == 1
            event, kwargs = posthog.events[0]
            assert event == "prescription_created"
            assert kwargs["distinct_id"] == str(driver.id)
            props = kwargs["properties"]
            assert props["prescription_id"] == str(pres.id)
            assert props["priority"] == "high"
            assert props["drill_template_id"] == "br.late_braking.v1"
    finally:
        await engine.dispose()


async def test_create_without_posthog_client_does_not_raise(
    upgraded_db_url: str,
) -> None:
    # Backward compat: T079's existing tests construct as
    # `PrescriptionRepo(db)` with no posthog. That path stays silent.
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            repo = PrescriptionRepo(db)
            pres = await repo.create(**_create_kwargs(driver.id))
            assert pres.id is not None
    finally:
        await engine.dispose()
