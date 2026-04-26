#!/usr/bin/env python
"""T236 — CLI: restore a daily pg_dump from R2 into a target DB.

Usage:
  python scripts/restore_db.py \
    --key pg-backups/2026-04-26/lap-backend.dump \
    --target postgresql://user:pw@host/dbname

Env vars (R2 connection — same names as the daily backup task):
  R2_BACKUP_BUCKET, R2_ENDPOINT_URL,
  R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY

The script is intentionally a thin wrapper around the pure
function in `infra/db_restore.py` — all the testable logic lives
there. This file just parses argv/env and connects the wires.

Operator notes (also in docs/error-budget.md restore drill):
- Run against a STAGING target before production unless this is
  a real incident. The --clean flag drops every existing object.
- pg_restore is read from $PATH; on the worker container it ships
  with the postgresql-client package.
"""

from __future__ import annotations

import argparse
import os
import sys

from infra.db_restore import restore_database_from_r2
from infra.storage import R2BlobStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Restore a pg_dump from R2 into a target database."
    )
    parser.add_argument(
        "--key",
        required=True,
        help="R2 object key (e.g. pg-backups/2026-04-26/lap-backend.dump)",
    )
    parser.add_argument(
        "--target",
        required=True,
        help="Target database URL (postgresql://...)",
    )
    args = parser.parse_args(argv)

    store = R2BlobStore(
        bucket=os.environ["R2_BACKUP_BUCKET"],
        endpoint_url=os.environ.get("R2_ENDPOINT_URL"),
        access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
    )
    restore_database_from_r2(
        key=args.key,
        target_database_url=args.target,
        store=store,
    )
    print(f"restored key={args.key} into target")
    return 0


if __name__ == "__main__":
    sys.exit(main())
