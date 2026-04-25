"""T179 — minimal Slack `chat.postMessage` poster.

Uses httpx (already a dep) instead of pulling in `slack_sdk`. The
factory wraps a bot token into a `SlackPoster` the cron task can
inject. Network failures bubble — beat will retry next Monday and
the no-token branch in `_send_weekly_kpi_report` already handles
the missing-token case.
"""

import httpx


class HttpSlackPoster:
    """Production `SlackPoster` — posts via Slack Web API.

    Token-only auth; one short-lived client per call so we don't
    hold a connection open between weekly fires.
    """

    _POST_URL = "https://slack.com/api/chat.postMessage"

    def __init__(self, token: str, *, timeout: float = 10.0) -> None:
        self._token = token
        self._timeout = timeout

    def post(self, channel: str, text: str) -> None:
        resp = httpx.post(
            self._POST_URL,
            headers={"Authorization": f"Bearer {self._token}"},
            json={"channel": channel, "text": text},
            timeout=self._timeout,
        )
        resp.raise_for_status()


def make_slack_poster(token: str) -> HttpSlackPoster:
    return HttpSlackPoster(token)
