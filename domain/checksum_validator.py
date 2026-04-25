"""Chunk sha256 validator (T116).

Used at the ingest boundary (T101) and by the Celery processing
pipeline (when it re-reads chunks from the blob store) to confirm a
chunk matches the digest the manifest claimed.

A mismatch raises `ChecksumMismatchError` carrying the filename so
operators can identify the bad chunk in logs.
"""

import hashlib


class ChecksumMismatchError(ValueError):
    """A chunk's sha256 didn't match the manifest's expected value."""


def verify_chunk_checksum(
    filename: str,
    payload: bytes,
    expected_checksums: dict[str, str],
) -> str:
    """Return the chunk's hex sha256.

    Raises ChecksumMismatchError if the manifest claims a digest for
    `filename` and the computed digest doesn't match.
    Returns the hex digest unconditionally so callers can persist it
    on the artifact row regardless of whether the manifest claimed it.
    """
    digest_hex = hashlib.sha256(payload).hexdigest()
    expected = expected_checksums.get(filename)
    if expected is not None and expected != f"sha256:{digest_hex}":
        raise ChecksumMismatchError(
            f"chunk_checksum_mismatch: {filename}"
        )
    return digest_hex
