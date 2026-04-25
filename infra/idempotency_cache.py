"""Redis-backed idempotency-key response cache (T099).

Stores the raw response bytes for a given Idempotency-Key with a 24h TTL.
A cache hit means the original request already succeeded, so the caller
can replay the cached body verbatim instead of re-running the
side-effect.
"""

from typing import Any

DEFAULT_IDEMPOTENCY_TTL_SECONDS = 24 * 60 * 60
KEY_PREFIX = "idem:"


class IdempotencyCache:
    def __init__(
        self,
        redis_client: Any,
        *,
        ttl_seconds: int = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._r = redis_client
        self._ttl = ttl_seconds

    @staticmethod
    def _key(idempotency_key: str) -> str:
        return f"{KEY_PREFIX}{idempotency_key}"

    def get(self, idempotency_key: str) -> bytes | None:
        raw = self._r.get(self._key(idempotency_key))
        if raw is None:
            return None
        if isinstance(raw, str):
            return raw.encode()
        assert isinstance(raw, bytes)
        return raw

    def put(self, idempotency_key: str, value: bytes) -> None:
        self._r.set(self._key(idempotency_key), value, ex=self._ttl)
