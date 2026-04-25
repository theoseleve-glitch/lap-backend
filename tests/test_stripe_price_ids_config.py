"""T147 — Stripe price IDs (LAP Monthly €15, LAP Yearly €150,
Waitlist Founder €12) are loadable from environment.

Product creation in the Stripe dashboard is a manual ops step.
Code-side, we only require that the IDs are read off `Config`.
"""

import pytest
from pydantic import SecretStr

from app.config import Config


def _minimal_config(**overrides: str) -> Config:
    base: dict[str, object] = {
        "database_url": "postgres://x",
        "redis_url": "redis://x",
        "clerk_secret": SecretStr("sk_test_clerk"),
        "stripe_secret": SecretStr("sk_test_stripe"),
    }
    base.update(overrides)
    return Config(**base)


def test_config_exposes_lap_monthly_price_id() -> None:
    cfg = _minimal_config(stripe_price_lap_monthly="price_test_lap_monthly")
    assert cfg.stripe_price_lap_monthly == "price_test_lap_monthly"


def test_config_exposes_lap_yearly_price_id() -> None:
    cfg = _minimal_config(stripe_price_lap_yearly="price_test_lap_yearly")
    assert cfg.stripe_price_lap_yearly == "price_test_lap_yearly"


def test_config_exposes_waitlist_founder_price_id() -> None:
    cfg = _minimal_config(
        stripe_price_waitlist_founder="price_test_waitlist_founder"
    )
    assert cfg.stripe_price_waitlist_founder == "price_test_waitlist_founder"


def test_price_id_fields_default_to_empty_string() -> None:
    # Empty defaults so dev/test environments don't need real IDs;
    # prod deploys must set them via env.
    cfg = _minimal_config()
    assert cfg.stripe_price_lap_monthly == ""
    assert cfg.stripe_price_lap_yearly == ""
    assert cfg.stripe_price_waitlist_founder == ""


def test_price_id_fields_load_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    monkeypatch.setenv("STRIPE_PRICE_LAP_MONTHLY", "price_env_monthly")
    monkeypatch.setenv("STRIPE_PRICE_LAP_YEARLY", "price_env_yearly")
    monkeypatch.setenv("STRIPE_PRICE_WAITLIST_FOUNDER", "price_env_founder")
    cfg = Config()
    assert cfg.stripe_price_lap_monthly == "price_env_monthly"
    assert cfg.stripe_price_lap_yearly == "price_env_yearly"
    assert cfg.stripe_price_waitlist_founder == "price_env_founder"


def test_env_example_documents_the_three_price_ids() -> None:
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    env_text = (repo_root / ".env.example").read_text(encoding="utf-8")
    assert "STRIPE_PRICE_LAP_MONTHLY" in env_text
    assert "STRIPE_PRICE_LAP_YEARLY" in env_text
    assert "STRIPE_PRICE_WAITLIST_FOUNDER" in env_text
