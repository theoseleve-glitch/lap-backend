"""T116 — chunk sha256 validator (domain layer).

T101 inlined this check in the ingest endpoint. T116 extracts it
into `domain/checksum_validator.py` so the same contract can run
from the Celery processing pipeline when it re-reads chunks from
the blob store.
"""

import hashlib

import pytest

from domain.checksum_validator import (
    ChecksumMismatchError,
    verify_chunk_checksum,
)


def _sha256(b: bytes) -> str:
    return f"sha256:{hashlib.sha256(b).hexdigest()}"


def test_matching_checksum_returns_hex_digest() -> None:
    payload = b"hello-world"
    digest_hex = verify_chunk_checksum(
        "samples.bin", payload, {"samples.bin": _sha256(payload)}
    )
    assert digest_hex == hashlib.sha256(payload).hexdigest()


def test_mismatched_checksum_raises() -> None:
    bad = "sha256:" + ("0" * 64)
    with pytest.raises(ChecksumMismatchError):
        verify_chunk_checksum("samples.bin", b"x", {"samples.bin": bad})


def test_filename_absent_from_manifest_returns_digest_no_error() -> None:
    payload = b"unverified"
    digest = verify_chunk_checksum("extra.bin", payload, {})
    # The manifest didn't claim a digest, so we trust the upload but
    # still compute and return one for downstream artifact rows.
    assert digest == hashlib.sha256(payload).hexdigest()


def test_empty_payload_with_matching_digest_passes() -> None:
    empty = b""
    digest_hex = verify_chunk_checksum(
        "empty.bin", empty, {"empty.bin": _sha256(empty)}
    )
    assert digest_hex == hashlib.sha256(empty).hexdigest()


def test_malformed_expected_format_treated_as_mismatch() -> None:
    # The manifest carries "sha256:<hex>" by spec. Anything else is
    # mismatch — we don't try to interpret alternative encodings.
    with pytest.raises(ChecksumMismatchError):
        verify_chunk_checksum(
            "samples.bin", b"data", {"samples.bin": "not-a-real-digest"}
        )


def test_expected_with_wrong_algorithm_prefix_treated_as_mismatch() -> None:
    # Some pipelines might naively send "md5:..." — the validator
    # rejects it rather than silently accept.
    with pytest.raises(ChecksumMismatchError):
        verify_chunk_checksum(
            "samples.bin",
            b"data",
            {"samples.bin": f"md5:{hashlib.md5(b'data').hexdigest()}"},  # noqa: S324
        )


def test_error_message_identifies_filename_and_kind() -> None:
    bad = "sha256:" + ("0" * 64)
    with pytest.raises(ChecksumMismatchError) as exc_info:
        verify_chunk_checksum("samples.bin", b"x", {"samples.bin": bad})
    msg = str(exc_info.value)
    # Operators reading logs need to know WHICH chunk failed.
    assert "samples.bin" in msg
