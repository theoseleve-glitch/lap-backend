"""T090 (sprint2-backend) — integration: prescription generator
applies target-aware priority bump.

Pure-domain priority function is tested in
test_target_aware_priority.py. This file pins the wiring: when
generate_prescription_for_episode is invoked with target_event
context AND the candidate prescription matches the target's
track or car, the persisted ActionPrescription.priority is
"p0" instead of the default "p1".

Backwards-compatible: existing callers that pass no target
context get the same "p1" they always did. The optional kwargs
on generate_prescription_for_episode mean test_prescription_*
tests stay green without modification.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import uuid6
import yaml
from alembic.config import Config as AlembicConfig
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from coaching.library.rule_schema import RuleSpec
from infra.db import make_engine, make_session_factory
from infra.orm import (
    ActionPrescription,
    DriverProfile,
    User,
    WeaknessEpisode,
)
from infra.prescription_generator import generate_prescription_for_episode

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def session_factory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[async_sessionmaker[AsyncSession], None, None]:
    db_path = tmp_path / "presc.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    factory = make_session_factory(engine)
    yield factory


_RULE_YAML = """
id: "br.late_braking.apex_speed_loss"
version: 1
category: technique
subcategory: late_braking
difficulty: beginner
detection:
  signal: apex_speed
  condition: "apex_speed < reference_apex_speed * 0.93"
  context:
    must_have_channels: ["brake"]
    min_corner_priority: medium
  evidence_required:
    min_occurrences_in_session: 3
    min_sessions_in_window: 2
    window_days: 14
severity_rule: "linear"
explanation_template:
  beginner: "b"
  intermediate: "i"
  advanced: "a"
prescription:
  drill_template_id: "drill.trail_brake_later_release"
  expected_gain_kmh: "2-5"
  success_condition:
    metric: "apex_speed"
    comparison: "gte"
    target_pct: 0.96
    measurement_window: "next_3_sessions"
"""


def _rule() -> RuleSpec:
    return RuleSpec.model_validate(yaml.safe_load(_RULE_YAML))


async def _seed_episode(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid6.UUID, WeaknessEpisode]:
    async with factory() as db:
        user = User(
            id=uuid6.uuid7(),
            clerk_user_id=f"u-{uuid6.uuid7().hex}",
            email=f"{uuid6.uuid7().hex}@t",
        )
        driver = DriverProfile(
            id=uuid6.uuid7(),
            user_id=user.id,
            display_name="t090",
        )
        now = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
        episode = WeaknessEpisode(
            id=uuid6.uuid7(),
            driver_id=driver.id,
            category="technique",
            subcategory="late_braking",
            corner_tag="T3",
            environment_scope=[],
            first_observed_at=now,
            last_observed_at=now,
            recurrence_count=2,
            sessions_affected=[],
            severity="medium",
            confidence=0.7,
            status="open",
            linked_prescriptions=[],
            linked_insights=[],
            library_rule_id="br.late_braking.apex_speed_loss",
        )
        db.add_all([user, driver, episode])
        await db.commit()
        await db.refresh(episode)
        return driver.id, episode


def _target(track_id: str | None, car_id: str | None) -> dict[str, object]:
    return {
        "id": str(uuid6.uuid7()),
        "name": "Spa 6h",
        "kind": "sim_event",
        "starts_at": "2026-05-15T10:00:00+00:00",
        "ends_at": None,
        "track_id": track_id,
        "car_id": car_id,
        "competitive_weight": 0.85,
        "notes": None,
    }


# -- backward-compat: no target ----------------------------------


def test_generator_without_target_context_still_writes_p1(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Caller that doesn't pass target_event/candidate_* gets the
    existing default p1 priority. Pin: T090's wiring must not
    silently bump every prescription to p0 — that would defeat
    the discrimination signal target-aware was meant to add."""

    async def run() -> None:
        _driver_id, episode = await _seed_episode(session_factory)
        async with session_factory() as db:
            now = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
            await generate_prescription_for_episode(
                db, episode=episode, rule=_rule(), now=now,
            )
            row = (
                await db.execute(select(ActionPrescription))
            ).scalar_one()
            assert row.priority == "p1"

    asyncio.run(run())


# -- target match → p0 -------------------------------------------


def test_generator_with_track_match_writes_p0(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Episode candidate track == target track → priority p0.
    Pin: this is the load-bearing happy path — without it,
    target-aware coaching has no concrete behavioral effect on
    the persisted plan."""

    async def run() -> None:
        _driver_id, episode = await _seed_episode(session_factory)
        async with session_factory() as db:
            now = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
            track_id = uuid6.uuid7()
            await generate_prescription_for_episode(
                db,
                episode=episode,
                rule=_rule(),
                now=now,
                target_event=_target(
                    track_id=str(track_id), car_id=str(uuid6.uuid7()),
                ),
                candidate_track_id=track_id,
                candidate_car_id=uuid6.uuid7(),
            )
            row = (
                await db.execute(select(ActionPrescription))
            ).scalar_one()
            assert row.priority == "p0"

    asyncio.run(run())


# -- target mismatch → p1 ----------------------------------------


def test_generator_with_target_but_no_match_writes_p1(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Driver has a pin but the prescription's track/car don't
    match it → priority stays p1. Pin: the bump must be
    target-AWARE, not target-EXISTS — a regression that bumped
    on any pinned target would fire false-positive p0s for
    every cross-context prescription."""

    async def run() -> None:
        _driver_id, episode = await _seed_episode(session_factory)
        async with session_factory() as db:
            now = datetime(2026, 4, 27, 10, 0, tzinfo=UTC)
            await generate_prescription_for_episode(
                db,
                episode=episode,
                rule=_rule(),
                now=now,
                target_event=_target(
                    track_id=str(uuid6.uuid7()),
                    car_id=str(uuid6.uuid7()),
                ),
                candidate_track_id=uuid6.uuid7(),
                candidate_car_id=uuid6.uuid7(),
            )
            row = (
                await db.execute(select(ActionPrescription))
            ).scalar_one()
            assert row.priority == "p1"

    asyncio.run(run())
