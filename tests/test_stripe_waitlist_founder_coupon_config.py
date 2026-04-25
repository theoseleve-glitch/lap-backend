"""T158 — `WAITLIST_FOUNDER` promotion code is loadable from
environment.

The Stripe coupon (20% off, 12-month repeating) is created
manually in the dashboard. This test pins the code-side
configurability so deploys can override the canonical code name
per environment without a code change.
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


def test_config_exposes_waitlist_founder_promotion_code() -> None:
    cfg = _minimal_config(
        stripe_promo_code_waitlist_founder="WAITLIST_FOUNDER_OVERRIDE"
    )
    assert (
        cfg.stripe_promo_code_waitlist_founder
        == "WAITLIST_FOUNDER_OVERRIDE"
    )


def test_promotion_code_defaults_to_canonical_name() -> None:
    # The default must match the dashboard-created code so the
    # canonical case "just works" without env-var configuration.
    cfg = _minimal_config()
    assert cfg.stripe_promo_code_waitlist_founder == "WAITLIST_FOUNDER"


def test_promotion_code_loads_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgres://x")
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    monkeypatch.setenv(
        "STRIPE_PROMO_CODE_WAITLIST_FOUNDER", "FOUNDERS_2026"
    )
    cfg = Config()
    assert cfg.stripe_promo_code_waitlist_founder == "FOUNDERS_2026"


def test_env_example_documents_the_promotion_code() -> None:
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    env_text = (repo_root / ".env.example").read_text(encoding="utf-8")
    assert "STRIPE_PROMO_CODE_WAITLIST_FOUNDER" in env_text
