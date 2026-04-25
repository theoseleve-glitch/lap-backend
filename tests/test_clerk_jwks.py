import httpx
import pytest

from infra.clerk import ClerkJwksClient

_JWKS_PAYLOAD = {
    "keys": [
        {
            "kty": "RSA",
            "kid": "ins_test_key_1",
            "use": "sig",
            "alg": "RS256",
            "n": "x" * 320,
            "e": "AQAB",
        }
    ]
}


def _counting_handler(
    counter: list[int], payload: dict[str, object] | None = None
) -> httpx.MockTransport:
    def handler(_request: httpx.Request) -> httpx.Response:
        counter.append(1)
        return httpx.Response(200, json=payload or _JWKS_PAYLOAD)

    return httpx.MockTransport(handler)


async def test_fetch_jwks_returns_parsed_payload() -> None:
    transport = _counting_handler([])
    client = ClerkJwksClient(
        "https://clerk.example.com", transport=transport
    )
    jwks = await client.fetch_jwks()
    assert "keys" in jwks
    assert jwks["keys"][0]["kid"] == "ins_test_key_1"


async def test_fetch_jwks_uses_well_known_path() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(200, json=_JWKS_PAYLOAD)

    transport = httpx.MockTransport(handler)
    client = ClerkJwksClient(
        "https://clerk.example.com", transport=transport
    )
    await client.fetch_jwks()
    assert seen_paths == ["/.well-known/jwks.json"]


async def test_fetch_jwks_caches_within_ttl() -> None:
    counter: list[int] = []
    transport = _counting_handler(counter)
    client = ClerkJwksClient(
        "https://clerk.example.com",
        ttl_seconds=3600,
        transport=transport,
    )
    await client.fetch_jwks()
    await client.fetch_jwks()
    await client.fetch_jwks()
    assert len(counter) == 1


async def test_fetch_jwks_refetches_when_ttl_zero() -> None:
    counter: list[int] = []
    transport = _counting_handler(counter)
    client = ClerkJwksClient(
        "https://clerk.example.com",
        ttl_seconds=0,
        transport=transport,
    )
    await client.fetch_jwks()
    await client.fetch_jwks()
    assert len(counter) == 2


async def test_fetch_jwks_raises_on_http_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    client = ClerkJwksClient(
        "https://clerk.example.com", transport=transport
    )
    with pytest.raises(httpx.HTTPStatusError):
        await client.fetch_jwks()


async def test_fetch_jwks_strips_trailing_slash_from_issuer() -> None:
    seen_paths: list[str] = []
    seen_hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        seen_hosts.append(request.url.host)
        return httpx.Response(200, json=_JWKS_PAYLOAD)

    transport = httpx.MockTransport(handler)
    client = ClerkJwksClient(
        "https://clerk.example.com/", transport=transport
    )
    await client.fetch_jwks()
    assert seen_hosts == ["clerk.example.com"]
    assert seen_paths == ["/.well-known/jwks.json"]
