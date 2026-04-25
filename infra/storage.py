"""Blob storage abstraction (T100).

`LocalBlobStore` is the filesystem-backed v1 implementation. T106 will
introduce an R2-backed implementation behind the same `BlobStoreProtocol`
so callers don't change.
"""

from pathlib import Path
from typing import Protocol


class BlobStoreProtocol(Protocol):
    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...


class LocalBlobStore:
    """Stores blobs as files under `root`. Keys are path fragments, but
    must resolve strictly inside `root` — a manifest from a hostile
    client cannot escape the storage root via `..` or absolute paths.
    """

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve_safe(self, key: str) -> Path:
        if not key:
            raise ValueError("key must not be empty")
        if key.startswith("/") or key.startswith("\\"):
            raise ValueError("key must be relative")
        target = (self._root / key).resolve()
        try:
            target.relative_to(self._root)
        except ValueError as exc:
            raise ValueError("key escapes storage root") from exc
        return target

    def put(self, key: str, data: bytes) -> None:
        path = self._resolve_safe(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes | None:
        path = self._resolve_safe(key)
        if not path.exists():
            return None
        return path.read_bytes()
