import asyncio
import time
from typing import Any

import httpx


class ClerkJwksClient:
    def __init__(
        self,
        issuer_url: str,
        *,
        ttl_seconds: int = 3600,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._issuer_url = issuer_url.rstrip("/")
        self._ttl = ttl_seconds
        self._transport = transport
        self._cached: dict[str, Any] | None = None
        self._cached_at: float | None = None
        self._lock = asyncio.Lock()

    @property
    def jwks_url(self) -> str:
        return f"{self._issuer_url}/.well-known/jwks.json"

    async def fetch_jwks(self) -> dict[str, Any]:
        async with self._lock:
            if (
                self._cached is not None
                and self._cached_at is not None
                and (time.monotonic() - self._cached_at) < self._ttl
            ):
                return self._cached
            async with httpx.AsyncClient(transport=self._transport) as client:
                response = await client.get(self.jwks_url)
                response.raise_for_status()
                payload: dict[str, Any] = response.json()
            self._cached = payload
            self._cached_at = time.monotonic()
            return payload
