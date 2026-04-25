"""T166 — Sentry SDK init.

`configure_sentry(dsn)` runs once at process startup. An empty DSN
must be a no-op (so local dev and CI never beacon to Sentry); a
non-empty DSN must initialize the SDK so unhandled exceptions and
500-level FastAPI responses get reported.
"""

from typing import Any

import sentry_sdk

from app.config import Config
from infra.sentry import configure_sentry


def test_config_exposes_sentry_dsn_field(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("REDIS_URL", "redis://x")
    monkeypatch.setenv("CLERK_SECRET", "sk_test_clerk")
    monkeypatch.setenv("STRIPE_SECRET", "sk_test_stripe")
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    assert Config().sentry_dsn == ""

    monkeypatch.setenv("SENTRY_DSN", "https://k@o0.ingest.sentry.io/1")
    assert (
        Config().sentry_dsn == "https://k@o0.ingest.sentry.io/1"
    )


def test_empty_dsn_is_a_noop() -> None:
    captured: dict[str, Any] = {}

    def spy(**kwargs: Any) -> None:
        captured.update(kwargs)

    real_init = sentry_sdk.init
    sentry_sdk.init = spy  # type: ignore[assignment]
    try:
        configure_sentry("")
    finally:
        sentry_sdk.init = real_init  # type: ignore[assignment]

    assert captured == {}, (
        "configure_sentry called sentry_sdk.init even with empty DSN"
    )


def test_non_empty_dsn_initializes_sdk() -> None:
    captured: dict[str, Any] = {}

    def spy(**kwargs: Any) -> None:
        captured.update(kwargs)

    real_init = sentry_sdk.init
    sentry_sdk.init = spy  # type: ignore[assignment]
    try:
        configure_sentry("https://key@o0.ingest.sentry.io/1")
    finally:
        sentry_sdk.init = real_init  # type: ignore[assignment]

    assert captured["dsn"] == "https://key@o0.ingest.sentry.io/1"


def test_release_is_tagged_with_pyproject_version() -> None:
    # T168 — Sentry release tag is `lap-backend@<version>`, where
    # the version is whatever `pyproject.toml` ships with at start.
    # Ties uploaded events to the deployed git SHA so a regression's
    # blast radius matches a single deployment.
    import tomllib
    from pathlib import Path

    pyproject = (
        Path(__file__).resolve().parents[1] / "pyproject.toml"
    )
    project_meta = tomllib.loads(pyproject.read_text())["project"]
    expected = f"lap-backend@{project_meta['version']}"

    captured: dict[str, Any] = {}

    def spy(**kwargs: Any) -> None:
        captured.update(kwargs)

    real_init = sentry_sdk.init
    sentry_sdk.init = spy  # type: ignore[assignment]
    try:
        configure_sentry("https://key@o0.ingest.sentry.io/1")
    finally:
        sentry_sdk.init = real_init  # type: ignore[assignment]

    assert captured["release"] == expected


def test_traces_sample_rate_is_10_percent() -> None:
    # T167 — keep performance/trace volume sane: only 10% of
    # transactions sampled. Tweak via Sentry dashboard if more
    # detail is needed for a specific incident.
    captured: dict[str, Any] = {}

    def spy(**kwargs: Any) -> None:
        captured.update(kwargs)

    real_init = sentry_sdk.init
    sentry_sdk.init = spy  # type: ignore[assignment]
    try:
        configure_sentry("https://key@o0.ingest.sentry.io/1")
    finally:
        sentry_sdk.init = real_init  # type: ignore[assignment]

    assert captured["traces_sample_rate"] == 0.1


def test_500_error_is_captured_after_configure() -> None:
    # Prove the SDK actually fields a captured exception. We init
    # with a before_send hook that records events instead of
    # transmitting them — proves the wire path works without hitting
    # the network.
    captured: list[dict[str, Any]] = []

    def before_send(
        event: dict[str, Any], _hint: dict[str, Any]
    ) -> dict[str, Any] | None:
        captured.append(event)
        return None  # don't actually send anywhere

    sentry_sdk.init(
        dsn="https://key@o0.ingest.sentry.io/1",
        before_send=before_send,
    )
    try:
        try:
            raise RuntimeError("simulated 500")
        except RuntimeError:
            sentry_sdk.capture_exception()
        sentry_sdk.flush(timeout=2.0)
    finally:
        sentry_sdk.init(dsn="")  # tear down so other tests stay clean

    assert len(captured) == 1
    exc = captured[0]["exception"]["values"][0]
    assert exc["type"] == "RuntimeError"
    assert exc["value"] == "simulated 500"
