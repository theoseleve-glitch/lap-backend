"""T048 (sprint2-backend) — Redis-backed bridge suggest-links cache.

Stores the JSON response bytes for `GET /api/v1/bridge/suggest_links`
keyed by the source session_id, with a 1-hour TTL. The endpoint is
read-only and idempotent for a given source — caching avoids the
repeated catalog joins on the bridge dashboard's choose-pair UI.

1h TTL is the staleness budget: new sessions added by the driver
become eligible candidates within ~1h. Shorter would defeat the
cache; longer would risk surfacing a stale candidate list after a
fresh upload.

Cache key includes the bridge-suggest namespace prefix so it can't
collide with idempotency / pairing keys in shared Redis.
"""

from typing import Any
from uuid import UUID

DEFAULT_SUGGEST_TTL_SECONDS = 60 * 60  # 1 hour
KEY_PREFIX = "bridge_suggest:"


class SuggestCache:
    def __init__(
        self,
        redis_client: Any,
        *,
        ttl_seconds: int = DEFAULT_SUGGEST_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._r = redis_client
        self._ttl = ttl_seconds

    @staticmethod
    def _key(session_id: UUID) -> str:
        return f"{KEY_PREFIX}{session_id}"

    def get(self, session_id: UUID) -> bytes | None:
        raw = self._r.get(self._key(session_id))
        if raw is None:
            return None
        if isinstance(raw, str):
            return raw.encode()
        assert isinstance(raw, bytes)
        return raw

    def put(self, session_id: UUID, value: bytes) -> None:
        self._r.set(self._key(session_id), value, ex=self._ttl)
