"""T012 (sprint2-backend) — nightly rule false-positive audit.

Wraps the existing `compute_high_false_positive_rules` aggregation
in a Slack-posting alert so admins get pinged when one or more
rules cross the FP threshold — without anyone having to visit the
admin UI.

Mirrors the T180 session_loss_audit shape:
- Silent on clean runs (no rules over threshold = no Slack post)
- Single multi-line post when there ARE flagged rules (one per
  rule_id with rate + counts)

T012's "surfaces high-FP rules to admin" outcome is the Slack
post. The HTTP admin endpoint (T206 / T013) remains the
on-demand surface; this is the proactive one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import uuid6
from alembic.config import Config as AlembicConfig

from alembic import command
from infra.coaching_flag_aggregation import send_high_fp_rules_alert
from infra.db import make_engine, make_session_factory
from infra.orm import CoachingInsight, DriverProfile, User


class _FakeSlack:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def post(self, channel: str, text: str) -> None:
        self.calls.append((channel, text))


REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[Any, None, None]:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    engine = make_engine(url)
    yield make_session_factory(engine)


async def _seed_user(factory: Any) -> dict[str, Any]:
    user_id = uuid6.uuid7()
    driver_id = uuid6.uuid7()
    async with factory() as db:
        db.add(
            User(
                id=user_id,
                clerk_user_id=f"clerk_{user_id}",
                email=f"u_{user_id}@t",
            )
        )
        db.add(
            DriverProfile(id=driver_id, user_id=user_id, display_name="A")
        )
        await db.commit()
    return {"user_id": user_id, "driver_id": driver_id}


async def _seed_insights(
    factory: Any,
    *,
    driver_id: Any,
    rule_id: str,
    total: int,
    flagged: int,
) -> None:
    sid = uuid6.uuid7()
    async with factory() as db:
        for i in range(total):
            db.add(
                CoachingInsight(
                    id=uuid6.uuid7(),
                    session_id=sid,
                    driver_id=driver_id,
                    created_at=datetime(2026, 4, 25, tzinfo=UTC),
                    rule_id=rule_id,
                    rule_version="1",
                    observation=f"o_{i}",
                    evidence=[],
                    severity="medium",
                    confidence=0.8,
                    flagged_false_positive=(i < flagged),
                )
            )
        await db.commit()


# -- behavior ------------------------------------------------------


def test_audit_silent_when_no_rules_above_threshold(
    factory: Any,
) -> None:
    """Empty insight table → no Slack post (silent on quiet days)."""
    slack = _FakeSlack()

    async def _go() -> None:
        async with factory() as db:
            await send_high_fp_rules_alert(
                db, slack, channel="#alerts"
            )

    asyncio.run(_go())
    assert slack.calls == []


def test_audit_silent_when_all_rules_under_threshold(
    factory: Any,
) -> None:
    """A 5%-flagged rule (under the 10% default threshold) does
    not trigger an alert."""
    seeded = asyncio.run(_seed_user(factory))
    asyncio.run(
        _seed_insights(
            factory,
            driver_id=seeded["driver_id"],
            rule_id="rule.healthy.v1",
            total=20,
            flagged=1,
        )
    )
    slack = _FakeSlack()

    async def _go() -> None:
        async with factory() as db:
            await send_high_fp_rules_alert(
                db, slack, channel="#alerts"
            )

    asyncio.run(_go())
    assert slack.calls == []


def test_audit_posts_alert_when_rule_exceeds_threshold(
    factory: Any,
) -> None:
    """A 50%-flagged rule (over the 10% default) gets surfaced."""
    seeded = asyncio.run(_seed_user(factory))
    asyncio.run(
        _seed_insights(
            factory,
            driver_id=seeded["driver_id"],
            rule_id="rule.bad.v1",
            total=10,
            flagged=5,
        )
    )
    slack = _FakeSlack()

    async def _go() -> None:
        async with factory() as db:
            await send_high_fp_rules_alert(
                db, slack, channel="#alerts"
            )

    asyncio.run(_go())
    assert len(slack.calls) == 1
    channel, text = slack.calls[0]
    assert channel == "#alerts"
    assert "rule.bad.v1" in text


def test_audit_post_includes_rate_and_counts(
    factory: Any,
) -> None:
    seeded = asyncio.run(_seed_user(factory))
    asyncio.run(
        _seed_insights(
            factory,
            driver_id=seeded["driver_id"],
            rule_id="rule.bad.v1",
            total=10,
            flagged=5,
        )
    )
    slack = _FakeSlack()

    async def _go() -> None:
        async with factory() as db:
            await send_high_fp_rules_alert(
                db, slack, channel="#alerts"
            )

    asyncio.run(_go())
    text = slack.calls[0][1]
    # 50% rate, 5/10 counts — the operator needs all three to triage
    assert "50" in text  # 50% (or 50.0%)
    assert "5" in text
    assert "10" in text


def test_audit_lists_all_high_fp_rules_in_one_post(
    factory: Any,
) -> None:
    """Multiple flagged rules → one post that mentions every rule
    so the operator gets the full picture in a single ping."""
    seeded = asyncio.run(_seed_user(factory))
    asyncio.run(
        _seed_insights(
            factory,
            driver_id=seeded["driver_id"],
            rule_id="rule.a.v1",
            total=10,
            flagged=8,
        )
    )
    asyncio.run(
        _seed_insights(
            factory,
            driver_id=seeded["driver_id"],
            rule_id="rule.b.v1",
            total=10,
            flagged=3,
        )
    )
    slack = _FakeSlack()

    async def _go() -> None:
        async with factory() as db:
            await send_high_fp_rules_alert(
                db, slack, channel="#alerts"
            )

    asyncio.run(_go())
    assert len(slack.calls) == 1
    text = slack.calls[0][1]
    assert "rule.a.v1" in text
    assert "rule.b.v1" in text


def test_celery_beat_schedule_includes_nightly_rule_fp_audit() -> None:
    """The 'runs nightly' half of T012: the beat schedule must
    register the audit task at a daily cadence so the alert
    actually fires in production."""
    from infra.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    assert "nightly-rule-fp-audit" in schedule
    entry = schedule["nightly-rule-fp-audit"]
    assert entry["task"] == "infra.celery_app.nightly_rule_fp_audit"
