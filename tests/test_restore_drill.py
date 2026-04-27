"""T185 (sprint2-backend) — restore-drill tests.

Pins the date math + wiring contract of `scripts/restore_drill.py`:
the drill must always pull *yesterday's* dump (not today's, not
the day before) using the same key format the dump task writes.

If either side of that contract changes — dump key format or
drill date math — these tests break loudly.

Pinned behaviors:
  - compute_yesterday_key subtracts exactly one day (not 0, not 2)
  - the date is formatted YYYY-MM-DD (matches the dump task)
  - month-boundary case (May 1 → April 30) is correct
  - year-boundary case (Jan 1 → Dec 31) is correct
  - default key prefix is "pg-backups" (matches the daily backup
    task — drift here would point the drill at empty space)
  - the prefix arg is honored when explicitly passed
  - `main()` invokes `restore_database_from_r2` with yesterday's
    key, against the env-driven target
  - missing required env var raises KeyError (loud failure)
  - the CLI script file exists and imports the same pure helper
    the tests cover
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.restore_drill import compute_yesterday_key, main

# -- pure-function: date math --------------------------------------


def test_compute_yesterday_key_subtracts_exactly_one_day() -> None:
    now = datetime(2026, 4, 27, 10, 30, tzinfo=UTC)
    key = compute_yesterday_key(now)
    assert key == "pg-backups/2026-04-26/lap-backend.dump"


def test_compute_yesterday_key_handles_month_boundary() -> None:
    """May 1 should resolve to April 30, not April 1 / May 0 /
    something else weird from naive integer subtraction."""
    now = datetime(2026, 5, 1, 0, 0, tzinfo=UTC)
    key = compute_yesterday_key(now)
    assert key == "pg-backups/2026-04-30/lap-backend.dump"


def test_compute_yesterday_key_handles_year_boundary() -> None:
    """Jan 1 should resolve to Dec 31 of the previous year."""
    now = datetime(2027, 1, 1, 0, 0, tzinfo=UTC)
    key = compute_yesterday_key(now)
    assert key == "pg-backups/2026-12-31/lap-backend.dump"


def test_compute_yesterday_key_default_prefix_matches_dump_task() -> None:
    """The default prefix must be 'pg-backups' — the same value
    the dump task at infra.db_backup uses. If they drift, the
    drill silently looks at the wrong directory."""
    now = datetime(2026, 4, 27, tzinfo=UTC)
    assert compute_yesterday_key(now).startswith("pg-backups/")


def test_compute_yesterday_key_honors_custom_prefix() -> None:
    now = datetime(2026, 4, 27, tzinfo=UTC)
    key = compute_yesterday_key(now, prefix="other-prefix")
    assert key == "other-prefix/2026-04-26/lap-backend.dump"


# -- main() wiring -------------------------------------------------


def test_main_invokes_restore_with_yesterday_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() must build the key from yesterday's UTC date and
    pass it through to restore_database_from_r2 along with the
    env-driven target URL. R2 store construction is verified by
    asserting it gets called — the actual creds are env-passthrough
    and not the contract under test here."""
    monkeypatch.setenv(
        "LAP_DRILL_TARGET_DATABASE_URL", "postgresql://drill-target/db"
    )
    monkeypatch.setenv("R2_BACKUP_BUCKET", "lap-backups-test")
    monkeypatch.setenv("R2_ENDPOINT_URL", "https://r2.test")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "AKIA-test")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret-test")

    fixed_now = datetime(2026, 4, 27, 12, 0, tzinfo=UTC)
    expected_key = "pg-backups/2026-04-26/lap-backend.dump"

    with (
        patch(
            "scripts.restore_drill.datetime",
            wraps=datetime,
        ) as dt_mock,
        patch(
            "scripts.restore_drill.restore_database_from_r2"
        ) as restore_mock,
        patch("scripts.restore_drill.R2BlobStore") as store_cls,
    ):
        dt_mock.now.return_value = fixed_now
        rc = main([])

    assert rc == 0
    assert store_cls.called
    restore_mock.assert_called_once()
    call_kwargs = restore_mock.call_args.kwargs
    assert call_kwargs["key"] == expected_key
    assert (
        call_kwargs["target_database_url"]
        == "postgresql://drill-target/db"
    )


def test_main_missing_target_env_raises_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No silent failure: if LAP_DRILL_TARGET_DATABASE_URL is unset,
    main() must raise so the operator sees the misconfiguration
    rather than getting a vacuous success log."""
    monkeypatch.delenv("LAP_DRILL_TARGET_DATABASE_URL", raising=False)
    with pytest.raises(KeyError, match="LAP_DRILL_TARGET_DATABASE_URL"):
        main([])


# -- script existence ----------------------------------------------


def test_drill_cli_script_exists() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "restore_drill.py"
    assert script.is_file(), f"missing CLI at {script}"


def test_drill_cli_imports_pure_helpers() -> None:
    """The CLI must use the same imports the tests cover —
    not its own ad-hoc reimplementation that would silently drift.
    """
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "restore_drill.py"
    body = script.read_text(encoding="utf-8")
    assert (
        "from infra.db_restore import restore_database_from_r2"
        in body
    )
    assert "from infra.storage import R2BlobStore" in body
