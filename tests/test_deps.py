import pytest

from app.config import Config
from app.deps import get_config, get_current_user, get_db


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://localhost:5432/lap")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")


def test_deps_are_callable() -> None:
    assert callable(get_config)
    assert callable(get_db)
    assert callable(get_current_user)


def test_get_config_returns_cached_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    get_config.cache_clear()
    first = get_config()
    second = get_config()
    assert isinstance(first, Config)
    assert first is second
    get_config.cache_clear()


