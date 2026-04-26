"""T052 (sprint2-backend) — cohort privacy enforcement.

Cohort benchmark calculations pool aggregate telemetry across
drivers (T054+). Only drivers who have explicitly opted in
(driver_profiles.cohort_opt_in = TRUE) are eligible to contribute.
Default for new accounts is FALSE — opt-in is the consent model,
not opt-out.

This module hosts the privacy gate. Two flavors:
  - is_eligible_for_cohort(bool) — pure predicate for in-memory
    decisions
  - cohort_eligible_drivers_filter() — SQLAlchemy filter for
    "only opt-in drivers" SELECT clauses

Both must agree, and any future cohort-data-consuming code must
funnel through one or the other so the privacy rule has a single
enforcement chokepoint.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import uuid6
from alembic.config import Config as AlembicConfig
from sqlalchemy import select

from alembic import command
from domain.cohort_eligibility import (
    cohort_eligible_drivers_filter,
    is_eligible_for_cohort,
)
from infra.db import make_engine, make_session_factory
from infra.orm import DriverProfile, User

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"


@pytest.fixture
def upgraded_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    db = tmp_path / "test.sqlite"
    url = f"sqlite+aiosqlite:///{db.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    command.upgrade(AlembicConfig(str(ALEMBIC_INI)), "head")
    return url


# -- pure predicate -----------------------------------------------


def test_default_false_is_not_eligible() -> None:
    """Default cohort_opt_in is False (per ORM server_default).
    A driver who hasn't toggled the setting is NOT eligible —
    silence is non-consent."""
    assert is_eligible_for_cohort(False) is False


def test_explicit_true_is_eligible() -> None:
    """Explicit opt-in → eligible. The only path to True."""
    assert is_eligible_for_cohort(True) is True


# -- SQL filter ---------------------------------------------------


async def _seed_drivers_with_mixed_consent(factory: object) -> dict[str, str]:
    """Two drivers: alice opted in, bob opted out (default)."""
    factory_callable = factory  # type: ignore[assignment]
    async with factory_callable() as db:  # type: ignore[operator]
        alice_user = User(
            id=uuid6.uuid7(), clerk_user_id="alice", email="a@t",
        )
        bob_user = User(
            id=uuid6.uuid7(), clerk_user_id="bob", email="b@t",
        )
        alice = DriverProfile(
            id=uuid6.uuid7(), user_id=alice_user.id, display_name="A",
            cohort_opt_in=True,
        )
        bob = DriverProfile(
            id=uuid6.uuid7(), user_id=bob_user.id, display_name="B",
            cohort_opt_in=False,
        )
        db.add_all([alice_user, bob_user, alice, bob])
        await db.commit()
    return {"alice_id": str(alice.id), "bob_id": str(bob.id)}


def test_filter_excludes_opt_out_drivers(upgraded_db_url: str) -> None:
    """Apply the SQL filter to a SELECT — only opt-in drivers come
    back. This is the production-path enforcement: every cohort
    query must add the filter, and this test pins it works."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            ids = await _seed_drivers_with_mixed_consent(factory)
            async with factory() as db:
                rows = (
                    (
                        await db.execute(
                            select(DriverProfile).where(
                                cohort_eligible_drivers_filter()
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                ids_returned = {str(r.id) for r in rows}
                assert ids["alice_id"] in ids_returned
                assert ids["bob_id"] not in ids_returned
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_filter_returns_empty_when_no_opt_in_drivers(
    upgraded_db_url: str,
) -> None:
    """Edge case: zero opt-in drivers in the system → query returns
    empty set, not an error. Prevents accidental "fall back to all
    drivers" implementations."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            async with factory() as db:
                bob_user = User(
                    id=uuid6.uuid7(), clerk_user_id="bob", email="b@t",
                )
                bob = DriverProfile(
                    id=uuid6.uuid7(), user_id=bob_user.id,
                    display_name="B", cohort_opt_in=False,
                )
                db.add_all([bob_user, bob])
                await db.commit()
                rows = (
                    (
                        await db.execute(
                            select(DriverProfile).where(
                                cohort_eligible_drivers_filter()
                            )
                        )
                    )
                    .scalars()
                    .all()
                )
                assert rows == []
        finally:
            await engine.dispose()

    asyncio.run(run())


# -- predicate / filter agreement ---------------------------------


def test_predicate_and_filter_agree(upgraded_db_url: str) -> None:
    """The pure predicate and SQL filter must classify the same set
    of drivers identically — pinning that the two enforcement paths
    don't diverge as either evolves."""

    async def run() -> None:
        engine = make_engine(upgraded_db_url)
        try:
            factory = make_session_factory(engine)
            await _seed_drivers_with_mixed_consent(factory)
            async with factory() as db:
                all_rows = (
                    (await db.execute(select(DriverProfile)))
                    .scalars()
                    .all()
                )
                via_predicate = {
                    str(r.id) for r in all_rows
                    if is_eligible_for_cohort(r.cohort_opt_in)
                }
                via_filter = {
                    str(r.id) for r in (
                        (
                            await db.execute(
                                select(DriverProfile).where(
                                    cohort_eligible_drivers_filter()
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                }
                assert via_predicate == via_filter
        finally:
            await engine.dispose()

    asyncio.run(run())
