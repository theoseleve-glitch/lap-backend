"""Pairing token stores.

`PairingStore` is the in-process dict-backed implementation kept for tests
that don't need cross-process state. `RedisPairingStore` is the production
backing introduced by T091: 5-minute TTL via Redis SET EX, single-use via
GETDEL atomicity.

Both classes expose the same `create(user_id) -> token` and
`consume(token) -> UUID | None` interface so callers (and FastAPI dep
overrides) can substitute one for the other.
"""

import secrets
from typing import Any, Protocol
from uuid import UUID

DEFAULT_PAIRING_TTL_SECONDS = 5 * 60
KEY_PREFIX = "pairing:"


class PairingStoreProtocol(Protocol):
    def create(self, user_id: UUID) -> str: ...
    def consume(self, token: str) -> UUID | None: ...


class PairingStore:
    """Process-local in-memory store. Used by tests; not for production."""

    def __init__(self) -> None:
        self._tokens: dict[str, UUID] = {}

    def create(self, user_id: UUID) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = user_id
        return token

    def consume(self, token: str) -> UUID | None:
        return self._tokens.pop(token, None)


class RedisPairingStore:
    """Redis-backed store. Production use. TTL + single-use via GETDEL."""

    def __init__(
        self,
        redis_client: Any,
        *,
        ttl_seconds: int = DEFAULT_PAIRING_TTL_SECONDS,
    ) -> None:
        self._r = redis_client
        self._ttl = ttl_seconds

    @staticmethod
    def _key(token: str) -> str:
        return f"{KEY_PREFIX}{token}"

    def create(self, user_id: UUID) -> str:
        token = secrets.token_urlsafe(32)
        self._r.set(self._key(token), str(user_id), ex=self._ttl)
        return token

    def consume(self, token: str) -> UUID | None:
        # GETDEL is atomic: returns the value AND deletes the key in one op.
        # That's exactly the single-use semantic — two concurrent redeems
        # cannot both win because Redis serializes the operation.
        raw = self._r.getdel(self._key(token))
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        return UUID(raw)
