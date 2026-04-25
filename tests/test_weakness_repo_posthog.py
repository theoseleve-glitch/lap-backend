"""T173 — `WeaknessRepo.resolve` emits a `weakness_resolved` event.

Closes the funnel started by `prescription_created` (T171) and
`prescription_accepted` (T172): once the episode flips to resolved
the work was successful and we can attribute the resolution to the
detecting rule.
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
from infra.weakness_repo import WeaknessRepo

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
    user = User(id=uuid6.uuid7(), clerk_user_id="u_t173", email="t173@t")
    driver = DriverProfile(
        id=uuid6.uuid7(), user_id=user.id, display_name="A"
    )
    db.add_all([user, driver])
    await db.commit()
    return driver


def _open_kwargs(driver_id: Any) -> dict[str, Any]:
    return {
        "driver_id": driver_id,
        "category": "braking",
        "subcategory": "late_braking",
        "severity": "moderate",
        "confidence": 0.85,
        "library_rule_id": "br.late_braking.apex_speed_loss.v1",
        "first_observed_at": datetime(2026, 4, 1, tzinfo=UTC),
        "last_observed_at": datetime(2026, 4, 20, tzinfo=UTC),
        "recurrence_count": 4,
    }


async def test_resolve_emits_weakness_resolved_event(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            posthog = RecordingPosthog()
            repo = WeaknessRepo(db, posthog_client=posthog)
            episode = await repo.open(**_open_kwargs(driver.id))

            resolved = await repo.resolve(
                episode.id,
                resolved_at=datetime(2026, 4, 25, tzinfo=UTC),
            )

            assert resolved is not None
            assert resolved.status == "resolved"
            assert len(posthog.events) == 1
            event, kwargs = posthog.events[0]
            assert event == "weakness_resolved"
            assert kwargs["distinct_id"] == str(driver.id)
            props = kwargs["properties"]
            assert props["episode_id"] == str(episode.id)
            assert props["category"] == "braking"
            assert props["subcategory"] == "late_braking"
            assert (
                props["library_rule_id"]
                == "br.late_braking.apex_speed_loss.v1"
            )
    finally:
        await engine.dispose()


async def test_resolve_unknown_episode_does_not_emit_event(
    upgraded_db_url: str,
) -> None:
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            await _seed_driver(db)
            posthog = RecordingPosthog()
            repo = WeaknessRepo(db, posthog_client=posthog)
            result = await repo.resolve(
                uuid6.uuid7(), resolved_at=datetime(2026, 4, 25, tzinfo=UTC)
            )
            assert result is None
            assert posthog.events == []
    finally:
        await engine.dispose()


async def test_update_confidence_does_not_emit_event(
    upgraded_db_url: str,
) -> None:
    # Confidence drift (T209's nightly decay) is too noisy to surface
    # as a product event — only durable resolution counts.
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            posthog = RecordingPosthog()
            repo = WeaknessRepo(db, posthog_client=posthog)
            episode = await repo.open(**_open_kwargs(driver.id))
            posthog.events.clear()

            await repo.update_confidence(episode.id, 0.5)
            assert posthog.events == []
    finally:
        await engine.dispose()


async def test_resolve_without_posthog_client_does_not_raise(
    upgraded_db_url: str,
) -> None:
    # Backward compat: T078's existing tests construct as
    # `WeaknessRepo(db)`. That path stays silent.
    engine = make_engine(upgraded_db_url)
    try:
        session_factory = make_session_factory(engine)
        async with session_factory() as db:
            driver = await _seed_driver(db)
            repo = WeaknessRepo(db)
            episode = await repo.open(**_open_kwargs(driver.id))
            resolved = await repo.resolve(
                episode.id, resolved_at=datetime(2026, 4, 25, tzinfo=UTC)
            )
            assert resolved is not None
    finally:
        await engine.dispose()
