"""T169 — PostHog Python SDK init.

`make_posthog_client(project_api_key, host)` returns a usable client.
Empty api_key → a no-op stand-in so dev and CI never beacon out;
non-empty key → a real `posthog.Posthog` instance ready to capture
events from T170+ event-emit tasks.
"""

from typing import Any

from app.config import Config
from infra.posthog import make_posthog_client


def test_config_exposes_posthog_fields(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    monkeypatch.delenv("POSTHOG_PROJECT_API_KEY", raising=False)
    monkeypatch.delenv("POSTHOG_HOST", raising=False)
    cfg = Config()
    assert cfg.posthog_project_api_key == ""
    assert cfg.posthog_host == "https://us.i.posthog.com"

    monkeypatch.setenv("POSTHOG_PROJECT_API_KEY", "phc_test_real")
    monkeypatch.setenv("POSTHOG_HOST", "https://eu.i.posthog.com")
    cfg2 = Config()
    assert cfg2.posthog_project_api_key == "phc_test_real"
    assert cfg2.posthog_host == "https://eu.i.posthog.com"


def test_empty_api_key_returns_a_noop_client() -> None:
    client = make_posthog_client(project_api_key="", host="ignored")
    # The no-op client must not raise from any of the methods our
    # event-emit tasks (T170+) will call. It also must not retain
    # a network handle.
    client.capture("noop_event", distinct_id="user_test", properties={"x": 1})
    client.set(distinct_id="user_test", properties={"plan": "free"})


def test_real_api_key_returns_posthog_client() -> None:
    client = make_posthog_client(
        project_api_key="phc_test_real",
        host="https://us.i.posthog.com",
    )
    # Duck-typed: the real client exposes `capture` and `set`
    # (PostHog 7.x replaced `identify` with `set`).
    assert callable(getattr(client, "capture", None))
    assert callable(getattr(client, "set", None))


def test_real_client_uses_provided_host() -> None:
    client = make_posthog_client(
        project_api_key="phc_test_real",
        host="https://eu.i.posthog.com",
    )
    # `posthog.Posthog` exposes `host` as an attribute we can inspect.
    assert getattr(client, "host", None) == "https://eu.i.posthog.com"
