import pytest
from pydantic import ValidationError

from app.config import Config


def _set_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://localhost:5432/lap")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")


def test_config_reads_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    cfg = Config()
    assert cfg.database_url == "postgres://localhost:5432/lap"
    assert cfg.redis_url == "redis://localhost:6379"
    assert cfg.clerk_secret.get_secret_value() == "sk_test_clerk"
    assert cfg.stripe_secret.get_secret_value() == "sk_test_stripe"


def test_config_missing_env_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("DATABASE_URL", "REDIS_URL", "CLERK_SECRET", "STRIPE_SECRET"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Config()


def test_config_secret_does_not_leak_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)
    cfg = Config()
    rendered = repr(cfg)
    assert "sk_test_clerk" not in rendered
    assert "sk_test_stripe" not in rendered
