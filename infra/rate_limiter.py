"""Fixed-window rate limiter backed by a Redis-compatible client.

Uses INCR + EXPIRE on a per-window key. Fixed-window has a known edge
case — a burst spanning the bucket boundary can briefly allow up to 2x
the limit — but it's the cheapest scheme to implement, deterministic,
and good enough for v1 abuse mitigation. Sliding-window or token-bucket
schemes can replace this transparently if/when the trade-off matters.
"""

import time
from typing import Any


class RateLimiter:
    def __init__(
        self, redis_client: Any, *, limit: int, window_seconds: int
    ) -> None:
        self._r = redis_client
        self._limit = limit
        self._window = window_seconds

    def check(self, key: str) -> bool:
        """Return True if the request is allowed, False if rate-limited."""
        bucket = int(time.time() // self._window)
        full_key = f"rl:{key}:{bucket}"
        count = self._r.incr(full_key)
        if count == 1:
            # Buffer the TTL slightly beyond the window so the key clears
            # cleanly even if the request arrives at the very end.
            self._r.expire(full_key, self._window + 5)
        return int(count) <= self._limit
