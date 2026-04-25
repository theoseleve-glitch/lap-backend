"""T146 — Stripe SDK setup. Verifies that the `stripe` package is
installed and that `stripe.api_key` is set from `Config.stripe_secret`
via `infra.stripe.configure_stripe`.
"""

import importlib

import pytest
from pydantic import SecretStr

from app.config import Config
from infra.stripe import configure_stripe


def test_stripe_package_is_importable() -> None:
    importlib.import_module("stripe")


def test_configure_stripe_sets_api_key_from_config() -> None:
    import stripe

    cfg = Config(
        database_url="postgres://x",
        redis_url="redis://x",
        clerk_secret=SecretStr("sk_test_clerk"),
        stripe_secret=SecretStr("sk_test_stripe_key_for_testing_only"),
    )
    configure_stripe(cfg)
    assert stripe.api_key == "sk_test_stripe_key_for_testing_only"


def test_configure_stripe_rejects_empty_secret() -> None:
    cfg = Config(
        database_url="postgres://x",
        redis_url="redis://x",
        clerk_secret=SecretStr("sk_test_clerk"),
        stripe_secret=SecretStr(""),
    )
    with pytest.raises(ValueError, match="empty"):
        configure_stripe(cfg)


def test_configure_stripe_does_not_log_the_key(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Convention: never write secrets to stdout/stderr. Belt-and-
    # braces against accidental `print(stripe.api_key)`.
    cfg = Config(
        database_url="postgres://x",
        redis_url="redis://x",
        clerk_secret=SecretStr("sk_test_clerk"),
        stripe_secret=SecretStr("sk_test_super_secret_DO_NOT_LOG_ME"),
    )
    configure_stripe(cfg)
    captured = capsys.readouterr()
    assert "sk_test_super_secret_DO_NOT_LOG_ME" not in captured.out
    assert "sk_test_super_secret_DO_NOT_LOG_ME" not in captured.err
