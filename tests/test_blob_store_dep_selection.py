"""T106 — `get_blob_store` chooses R2 when configured, else local.

When R2_BUCKET (and friends) are set in env, the dependency returns an
`R2BlobStore`. Otherwise it falls back to `LocalBlobStore`. Tests
sidestep `lru_cache` by clearing it explicitly.
"""

from pathlib import Path

import pytest

from app.deps import get_blob_store, get_config
from infra.storage import LocalBlobStore, R2BlobStore


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    # The deps are lru_cached, so each test must start with a clean
    # slate or it'll see the prior test's instance.
    get_blob_store.cache_clear()
    get_config.cache_clear()


def test_returns_local_blob_store_when_no_r2_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("CLERK_SECRET", "x")
    monkeypatch.setenv("STRIPE_SECRET", "x")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "blobs"))
    monkeypatch.delenv("R2_BUCKET", raising=False)
    store = get_blob_store()
    assert isinstance(store, LocalBlobStore)


def test_returns_r2_blob_store_when_r2_bucket_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("CLERK_SECRET", "x")
    monkeypatch.setenv("STRIPE_SECRET", "x")
    monkeypatch.setenv("R2_BUCKET", "my-bucket")
    monkeypatch.setenv(
        "R2_ENDPOINT_URL", "https://acc.r2.cloudflarestorage.com"
    )
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "sk")
    store = get_blob_store()
    assert isinstance(store, R2BlobStore)


def test_empty_r2_bucket_falls_back_to_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty string in the env should not enable R2 — that would make
    # missing env vars silently flip behavior.
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("CLERK_SECRET", "x")
    monkeypatch.setenv("STRIPE_SECRET", "x")
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path / "blobs"))
    monkeypatch.setenv("R2_BUCKET", "")
    store = get_blob_store()
    assert isinstance(store, LocalBlobStore)
