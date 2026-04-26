"""T235 — daily pg_dump → R2 backup.

Pure function: takes a database URL, runs pg_dump in custom
format, uploads the dump bytes to a blob store under a dated
key. Dependency-injected so tests substitute a fake subprocess
runner and a LocalBlobStore instead of shelling out to real
Postgres or talking to real R2.

Custom format (pg_dump -Fc) is used because:
- restorable selectively (pg_restore -t table)
- supports parallel restore on multi-CPU machines
- compressed by default
A plain SQL dump would also work but loses these recovery options.

Dated keys (`<prefix>/<YYYY-MM-DD>/lap-backend.dump`) mean daily
runs don't overwrite each other. R2 lifecycle rules (configured
out-of-band) handle retention pruning.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import datetime
from typing import Any, Protocol


class _StoreLike(Protocol):
    def put(self, key: str, data: bytes) -> None: ...


def dump_database_to_r2(
    *,
    database_url: str,
    key_prefix: str,
    store: _StoreLike,
    now: datetime,
    subprocess_run: Callable[..., Any] = subprocess.run,
    pg_dump_path: str = "pg_dump",
) -> str:
    """Run pg_dump against `database_url` and upload the result.

    Returns the storage key the dump was written to so callers can
    log it (and ops can locate the artifact for restore drills).
    """
    completed = subprocess_run(
        [pg_dump_path, "--format=custom", database_url],
        check=True,
        capture_output=True,
    )
    key = f"{key_prefix}/{now.strftime('%Y-%m-%d')}/lap-backend.dump"
    store.put(key, completed.stdout)
    return key
