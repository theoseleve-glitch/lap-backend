#!/usr/bin/env python
"""T185 — weekly backup-restore drill.

Pulls **yesterday's** pg_dump from R2 and restores it into the
drill target database (typically a throwaway staging DB). Run on
a schedule so that the moment our daily backup task ever silently
breaks, this drill catches it within 24 hours rather than at the
next real incident.

Usage:
  python scripts/restore_drill.py

Required env vars:
  LAP_DRILL_TARGET_DATABASE_URL   target DB to restore into
                                   (must be a non-prod URL — the
                                   --clean flag drops every object)
  R2_BACKUP_BUCKET                 same bucket as the daily dump
  R2_ENDPOINT_URL                  R2 endpoint (https://...)
  R2_ACCESS_KEY_ID                 R2 creds with read access to
                                   the backup bucket
  R2_SECRET_ACCESS_KEY

Operator notes:
- Yesterday is UTC-yesterday, matching the dump's key format.
- A missing yesterday-key (e.g., the daily backup task didn't
  run) raises FileNotFoundError immediately so the failure is
  loud and obvious.
- Use a dedicated drill database (e.g., `lap_drill`) — never
  point this at staging-prod-traffic or production.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

from infra.db_restore import restore_database_from_r2
from infra.storage import R2BlobStore

DEFAULT_KEY_PREFIX = "pg-backups"


def compute_yesterday_key(
    now: datetime, prefix: str = DEFAULT_KEY_PREFIX
) -> str:
    """Build the R2 key for **yesterday's** dump relative to `now`.

    The dump task writes `{prefix}/{YYYY-MM-DD}/lap-backend.dump`
    using the date of the run. The drill always wants yesterday's
    file because today's dump may not have run yet (or may still
    be uploading) at drill time.
    """
    yesterday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    return f"{prefix}/{yesterday}/lap-backend.dump"


def main(argv: list[str] | None = None) -> int:
    del argv  # the drill takes no CLI args — env-only
    target = os.environ["LAP_DRILL_TARGET_DATABASE_URL"]
    store = R2BlobStore(
        bucket=os.environ["R2_BACKUP_BUCKET"],
        endpoint_url=os.environ.get("R2_ENDPOINT_URL"),
        access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )
    key = compute_yesterday_key(datetime.now(UTC))
    restore_database_from_r2(
        key=key, target_database_url=target, store=store
    )
    print(f"drill restored key={key}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
