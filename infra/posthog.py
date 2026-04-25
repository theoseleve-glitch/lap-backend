"""PostHog Python SDK setup (T169).

`make_posthog_client(project_api_key, host)` returns a usable client.
Empty api_key → a no-op stand-in so dev and CI never network out;
non-empty → a real `posthog.Posthog` instance ready for T170+
event-emit tasks. Pass through a single factory keeps the empty-
key branch out of every call site.
"""

from typing import Any, Protocol

from posthog import Posthog


class PosthogClientProtocol(Protocol):
    """Minimal surface our event-emit code depends on. Mirrors the
    PostHog 7.x API: `capture(event, ...)` for product events,
    `set(...)` for person-property updates (replaces 3.x's
    `identify`)."""

    def capture(self, event: str, **kwargs: Any) -> Any: ...

    def set(self, **kwargs: Any) -> Any: ...


class _NoOpPosthog:
    """Stand-in returned when the project API key is unset."""

    host: str = ""

    def capture(self, event: str, **kwargs: Any) -> None:  # noqa: ARG002
        return None

    def set(self, **kwargs: Any) -> None:  # noqa: ARG002
        return None


def make_posthog_client(
    project_api_key: str,
    host: str = "https://us.i.posthog.com",
) -> PosthogClientProtocol:
    """Return a real Posthog client when an API key is provided,
    otherwise a no-op duck-typed stand-in. Tests and dev runs land
    on the stand-in; deploys set POSTHOG_PROJECT_API_KEY and get
    real event delivery."""
    if not project_api_key:
        return _NoOpPosthog()
    return Posthog(project_api_key=project_api_key, host=host)
