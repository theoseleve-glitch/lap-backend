"""T236 — restore-from-backup script for T235's pg_dumps.

The pure function downloads a custom-format dump from R2 by key,
writes it to a temp file (pg_restore needs a file path for
custom format), invokes pg_restore against the target database,
and cleans up the temp file regardless of success or failure.

The script `scripts/restore_db.py` is the CLI surface that wires
env vars + argparse onto the pure function so an operator can run
it locally in an incident.

We can't run pg_restore for real in CI (no postgres binary on the
ubuntu-latest runner without setup, and we'd need a writable
target db). The round-trip test below proves the key/format
contract between T235 (dump → R2) and T236 (R2 → restore) is
mutual: dump writes bytes X to key K, restore reads bytes X from
key K and feeds them to pg_restore.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from infra.db_backup import dump_database_to_r2
from infra.db_restore import restore_database_from_r2
from infra.storage import LocalBlobStore, R2BlobStore

# -- fixtures ------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> LocalBlobStore:
    return LocalBlobStore(tmp_path / "blobs")


def _fake_run(returncode: int = 0) -> MagicMock:
    return MagicMock(
        return_value=CompletedProcess(
            args=[], returncode=returncode, stdout=b"", stderr=b""
        )
    )


# -- pure-function behavior ----------------------------------------


def test_restore_invokes_pg_restore_with_target_dbname(
    store: LocalBlobStore,
) -> None:
    store.put("pg-backups/2026-04-26/lap-backend.dump", b"DUMPBYTES")
    fake_run = _fake_run()
    restore_database_from_r2(
        key="pg-backups/2026-04-26/lap-backend.dump",
        target_database_url="postgresql://restore-target/db",
        store=store,
        subprocess_run=fake_run,
    )
    invocation_args = fake_run.call_args[0][0]
    assert invocation_args[0] == "pg_restore"
    assert any(
        "--dbname=postgresql://restore-target/db" in a
        for a in invocation_args
    )


def test_restore_passes_safety_flags(store: LocalBlobStore) -> None:
    """`--clean --if-exists --no-owner` is the standard "restore
    into a possibly-already-populated target" combo:
    - --clean drops existing objects so we don't get ALREADY_EXISTS errors
    - --if-exists tolerates missing objects on the drop pass
    - --no-owner skips OWNER TO clauses that wouldn't apply across envs
    Test pins all three so a future "let's strip the safety flags
    to keep restore simpler" edit fails."""
    store.put("k.dump", b"DUMPBYTES")
    fake_run = _fake_run()
    restore_database_from_r2(
        key="k.dump",
        target_database_url="postgresql://t",
        store=store,
        subprocess_run=fake_run,
    )
    invocation_args = fake_run.call_args[0][0]
    assert "--clean" in invocation_args
    assert "--if-exists" in invocation_args
    assert "--no-owner" in invocation_args


def test_restore_writes_dump_bytes_to_temp_file(
    store: LocalBlobStore, tmp_path: Path
) -> None:
    """The dump bytes from R2 must reach pg_restore unmodified.
    We capture the temp file path passed to pg_restore and read
    it back to verify the bytes round-tripped intact."""
    store.put("k.dump", b"BYTES-X-Y-Z")
    captured: dict[str, bytes] = {}

    def capture(args: list[str], **_kw: object) -> CompletedProcess[bytes]:
        # pg_restore's positional arg is the dump file path
        dump_path = Path(args[-1])
        captured["bytes"] = dump_path.read_bytes()
        return CompletedProcess(args=args, returncode=0, stdout=b"")

    restore_database_from_r2(
        key="k.dump",
        target_database_url="postgresql://t",
        store=store,
        subprocess_run=capture,
        tmp_dir=tmp_path,
    )
    assert captured["bytes"] == b"BYTES-X-Y-Z"


def test_restore_raises_when_key_missing(
    store: LocalBlobStore,
) -> None:
    """Restoring a missing dump must NOT silently no-op — that
    would let an incident operator believe the restore succeeded
    when nothing actually happened."""
    fake_run = _fake_run()
    with pytest.raises(FileNotFoundError, match="no backup"):
        restore_database_from_r2(
            key="pg-backups/9999-01-01/missing.dump",
            target_database_url="postgresql://t",
            store=store,
            subprocess_run=fake_run,
        )
    # pg_restore must NOT have been called when the key is absent.
    fake_run.assert_not_called()


def test_restore_cleans_up_temp_file_on_success(
    store: LocalBlobStore, tmp_path: Path
) -> None:
    store.put("k.dump", b"DATA")
    captured_paths: list[Path] = []

    def capture(args: list[str], **_kw: object) -> CompletedProcess[bytes]:
        captured_paths.append(Path(args[-1]))
        return CompletedProcess(args=args, returncode=0, stdout=b"")

    restore_database_from_r2(
        key="k.dump",
        target_database_url="postgresql://t",
        store=store,
        subprocess_run=capture,
        tmp_dir=tmp_path,
    )
    assert captured_paths and not captured_paths[0].exists(), (
        "temp file must be deleted after restore"
    )


def test_restore_cleans_up_temp_file_on_failure(
    store: LocalBlobStore, tmp_path: Path
) -> None:
    store.put("k.dump", b"DATA")
    captured_paths: list[Path] = []

    def boom(args: list[str], **_kw: object) -> CompletedProcess[bytes]:
        captured_paths.append(Path(args[-1]))
        raise RuntimeError("pg_restore exploded")

    with pytest.raises(RuntimeError, match="pg_restore"):
        restore_database_from_r2(
            key="k.dump",
            target_database_url="postgresql://t",
            store=store,
            subprocess_run=boom,
            tmp_dir=tmp_path,
        )
    assert captured_paths and not captured_paths[0].exists(), (
        "temp file must be deleted even when pg_restore fails"
    )


# -- T235/T236 round-trip via moto --------------------------------


@mock_aws
def test_dump_then_restore_round_trip_via_moto() -> None:
    """The whole point of the backup system is that T235's output
    is T236's input. This test stands up a moto-mocked S3 (which
    R2 is API-compatible with), runs the dump function, then runs
    the restore function, and asserts pg_restore got handed back
    the exact bytes that pg_dump produced. If the key format ever
    diverges between the two modules, this test breaks."""
    bucket = "lap-restore-test"
    s3 = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )
    s3.create_bucket(Bucket=bucket)
    store = R2BlobStore(
        bucket=bucket,
        endpoint_url=None,
        access_key_id="testing",
        secret_access_key="testing",
    )

    fake_dump = MagicMock(
        return_value=CompletedProcess(
            args=[], returncode=0, stdout=b"PG-DUMP-PAYLOAD"
        )
    )
    written_key = dump_database_to_r2(
        database_url="postgresql://src",
        key_prefix="pg-backups",
        store=store,
        now=datetime(2026, 4, 26, 2, 0, tzinfo=UTC),
        subprocess_run=fake_dump,
    )

    captured: dict[str, bytes] = {}

    def capture(args: list[str], **_kw: object) -> CompletedProcess[bytes]:
        captured["bytes"] = Path(args[-1]).read_bytes()
        return CompletedProcess(args=args, returncode=0, stdout=b"")

    restore_database_from_r2(
        key=written_key,
        target_database_url="postgresql://dst",
        store=store,
        subprocess_run=capture,
    )
    assert captured["bytes"] == b"PG-DUMP-PAYLOAD"


# -- CLI script surface --------------------------------------------


def test_restore_cli_script_exists() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "restore_db.py"
    assert script.is_file(), f"missing CLI at {script}"


def test_restore_cli_imports_pure_function() -> None:
    """The CLI must use the same pure function the tests cover —
    not its own ad-hoc reimplementation that would silently drift."""
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "scripts" / "restore_db.py"
    body = script.read_text(encoding="utf-8")
    assert "from infra.db_restore import restore_database_from_r2" in body
