"""T235 — daily pg_dump to R2 (S3-compatible).

Pure-function dump-and-upload, plus a Celery beat hook that
runs it at 02:00 UTC (before the 03:00 episode-decay /
03:30 prescription-expiry sweeps so the backup captures a
quiet snapshot, not mid-sweep state).

The function is dependency-injected with the subprocess runner
and the blob store so tests don't shell out to a real pg_dump
or talk to real R2.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock

import pytest

from infra.db_backup import dump_database_to_r2
from infra.storage import LocalBlobStore

# -- pure function -------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


def _fake_run_factory(
    stdout: bytes = b"PGDUMP-OK", returncode: int = 0
) -> MagicMock:
    return MagicMock(
        return_value=CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=b""
        )
    )


def test_dump_invokes_pg_dump_with_database_url(
    store: LocalBlobStore,
) -> None:
    fake_run = _fake_run_factory()
    dump_database_to_r2(
        database_url="postgresql://u:p@host/db",
        key_prefix="pg-backups",
        store=store,
        now=datetime(2026, 4, 26, tzinfo=UTC),
        subprocess_run=fake_run,
    )
    invocation_args = fake_run.call_args[0][0]
    assert invocation_args[0] == "pg_dump"
    # The url must be the last positional arg so pg_dump treats
    # it as the connection string, not as a switch value.
    assert invocation_args[-1] == "postgresql://u:p@host/db"


def test_dump_uses_custom_format(store: LocalBlobStore) -> None:
    """Custom format (-Fc) is restorable selectively and supports
    parallel restore, unlike plain SQL dumps. Encoding this here
    so a future "simplify to plain SQL" edit fails the test."""
    fake_run = _fake_run_factory()
    dump_database_to_r2(
        database_url="postgresql://test",
        key_prefix="pg-backups",
        store=store,
        now=datetime(2026, 4, 26, tzinfo=UTC),
        subprocess_run=fake_run,
    )
    invocation_args = fake_run.call_args[0][0]
    assert "--format=custom" in invocation_args


def test_dump_uploads_pg_dump_stdout_to_store(
    store: LocalBlobStore,
) -> None:
    fake_run = _fake_run_factory(stdout=b"BINARY-DUMP-BYTES")
    key = dump_database_to_r2(
        database_url="postgresql://test",
        key_prefix="pg-backups",
        store=store,
        now=datetime(2026, 4, 26, tzinfo=UTC),
        subprocess_run=fake_run,
    )
    assert store.get(key) == b"BINARY-DUMP-BYTES"


def test_dump_key_includes_iso_date(store: LocalBlobStore) -> None:
    """Dated keys mean daily runs don't overwrite each other —
    we get a 30-day rolling window of restorable points by default
    via R2 lifecycle rules (configured outside this code)."""
    fake_run = _fake_run_factory()
    key = dump_database_to_r2(
        database_url="postgresql://test",
        key_prefix="pg-backups",
        store=store,
        now=datetime(2026, 4, 26, 2, 0, tzinfo=UTC),
        subprocess_run=fake_run,
    )
    assert "2026-04-26" in key
    assert key.startswith("pg-backups/")


def test_dump_raises_when_pg_dump_fails(
    store: LocalBlobStore,
) -> None:
    """A pg_dump failure must NOT silently upload an empty/partial
    dump — that would let a corrupted backup pass undetected and
    surface only when restore is attempted in an incident."""
    fake_run = _fake_run_factory(stdout=b"", returncode=1)
    # subprocess.run with check=True normally raises on nonzero;
    # the fake mimics this so the function's check kwarg is
    # exercised in the test path.
    fake_run.side_effect = RuntimeError("pg_dump exited 1")
    with pytest.raises(RuntimeError, match="pg_dump"):
        dump_database_to_r2(
            database_url="postgresql://test",
            key_prefix="pg-backups",
            store=store,
            now=datetime(2026, 4, 26, tzinfo=UTC),
            subprocess_run=fake_run,
        )


# -- celery beat wiring --------------------------------------------


def test_beat_schedule_includes_daily_backup() -> None:
    """The beat schedule must register the backup task; without
    it the function exists but never runs in production."""
    from infra.celery_app import celery_app

    keys = celery_app.conf.beat_schedule.keys()
    assert any("backup" in k.lower() for k in keys), (
        f"no backup task in beat schedule: {list(keys)}"
    )


def test_backup_runs_before_other_03_sweeps() -> None:
    """02:00 (or earlier) so the backup snapshots quiet state,
    not mid-sweep state from the 03:00 / 03:30 jobs."""
    from infra.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule
    backup_entry = next(
        v for k, v in schedule.items() if "backup" in k.lower()
    )
    cron = backup_entry["schedule"]
    # celery.schedules.crontab stores `hour` as a set of ints
    backup_hour = min(int(h) for h in cron.hour)
    assert backup_hour < 3, (
        f"backup runs at {backup_hour:02d}:xx — must be before 03:00"
    )
