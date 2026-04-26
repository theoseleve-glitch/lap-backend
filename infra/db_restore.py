"""T236 — restore a T235 pg_dump from R2 into a target database.

Pure function: download the dump bytes from R2 by key, write to a
temp file (pg_restore needs a file path, not stdin, for custom
format), invoke pg_restore against the target, clean up the temp
file regardless of outcome.

The restore command uses three safety flags:
- --clean       drop existing objects before recreating
- --if-exists   tolerate missing objects on the drop pass
- --no-owner    skip OWNER TO clauses that wouldn't apply across envs

These together let the script restore into a possibly-already-
populated target without crashing on conflicts. Restoring into an
empty database is also fine — `--if-exists` makes the drops no-op.

The CLI surface lives at `scripts/restore_db.py`.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol


class _StoreLike(Protocol):
    def get(self, key: str) -> bytes | None: ...


def restore_database_from_r2(
    *,
    key: str,
    target_database_url: str,
    store: _StoreLike,
    subprocess_run: Callable[..., Any] = subprocess.run,
    pg_restore_path: str = "pg_restore",
    tmp_dir: Path | None = None,
) -> None:
    """Download the dump at `key` and pg_restore it into the target.

    Raises FileNotFoundError if `key` doesn't exist in the store —
    we never want a "restore" that silently no-ops, because an
    operator running this in an incident has to *know* whether the
    restore actually happened.
    """
    data = store.get(key)
    if data is None:
        raise FileNotFoundError(f"no backup at key {key!r}")

    # mkstemp returns an open fd; close it immediately so the
    # subsequent write_bytes() owns the file cleanly (matters on
    # Windows where double-handle to same path is unhappy).
    fd, tmp_name = tempfile.mkstemp(
        suffix=".dump",
        dir=str(tmp_dir) if tmp_dir is not None else None,
    )
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_bytes(data)
        subprocess_run(
            [
                pg_restore_path,
                "--clean",
                "--if-exists",
                "--no-owner",
                f"--dbname={target_database_url}",
                str(tmp_path),
            ],
            check=True,
        )
    finally:
        tmp_path.unlink(missing_ok=True)
