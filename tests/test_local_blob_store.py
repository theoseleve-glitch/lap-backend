"""Unit tests for the local-filesystem blob store (T100).

Local fs is the v1 implementation; T106 swaps the backing to Cloudflare
R2 behind the same `BlobStore` protocol so callers don't change.
"""

from pathlib import Path

import pytest

from infra.storage import BlobStoreProtocol, LocalBlobStore


def test_put_writes_bytes_to_disk(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put("a/b/c.bin", b"hello")
    assert (tmp_path / "a" / "b" / "c.bin").read_bytes() == b"hello"


def test_get_returns_written_bytes(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put("k.bin", b"payload")
    assert store.get("k.bin") == b"payload"


def test_get_unknown_key_returns_none(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    assert store.get("nope.bin") is None


def test_put_creates_intermediate_directories(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put("deeply/nested/path/file.bin", b"x")
    assert (tmp_path / "deeply" / "nested" / "path" / "file.bin").exists()


def test_put_overwrites_existing_value(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put("k.bin", b"first")
    store.put("k.bin", b"second")
    assert store.get("k.bin") == b"second"


def test_distinct_keys_persist_independently(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path)
    store.put("a.bin", b"A")
    store.put("b.bin", b"B")
    assert store.get("a.bin") == b"A"
    assert store.get("b.bin") == b"B"


def test_local_store_implements_blob_store_protocol(tmp_path: Path) -> None:
    # Structural check — the protocol is the abstraction R2 will satisfy.
    store: BlobStoreProtocol = LocalBlobStore(tmp_path)
    assert hasattr(store, "put")
    assert hasattr(store, "get")


@pytest.mark.parametrize(
    "bad_key",
    [
        "../escape.bin",
        "a/../../escape.bin",
        "/abs.bin",
        "",
    ],
)
def test_put_rejects_unsafe_keys(tmp_path: Path, bad_key: str) -> None:
    # Path traversal would let a malicious manifest write outside the
    # storage root. Reject anything that doesn't resolve under root.
    store = LocalBlobStore(tmp_path)
    with pytest.raises(ValueError):
        store.put(bad_key, b"x")
