"""Unit tests for the Redis-backed idempotency cache (T099).

The cache stores the canonical response bytes for a given Idempotency-Key
so repeated requests can be replayed without re-executing the side-effect.
24h TTL is the v1 retention horizon — long enough that retries from
flaky networks land safely, short enough that the keyspace stays bounded.
"""

import fakeredis
import pytest

from infra.idempotency_cache import (
    DEFAULT_IDEMPOTENCY_TTL_SECONDS,
    IdempotencyCache,
)


def _cache(ttl: int = DEFAULT_IDEMPOTENCY_TTL_SECONDS) -> IdempotencyCache:
    return IdempotencyCache(fakeredis.FakeRedis(), ttl_seconds=ttl)


def test_get_unknown_key_returns_none() -> None:
    cache = _cache()
    assert cache.get("never-seen") is None


def test_put_then_get_returns_value_bytes() -> None:
    cache = _cache()
    cache.put("k1", b'{"status":"accepted"}')
    assert cache.get("k1") == b'{"status":"accepted"}'


def test_default_ttl_is_24_hours() -> None:
    assert DEFAULT_IDEMPOTENCY_TTL_SECONDS == 24 * 60 * 60


def test_put_sets_configured_ttl_in_redis() -> None:
    redis = fakeredis.FakeRedis()
    cache = IdempotencyCache(redis, ttl_seconds=86400)
    cache.put("k1", b"x")
    ttl = redis.ttl("idem:k1")
    # Allow a small slack window since the call is not instantaneous.
    assert 86390 <= ttl <= 86400


def test_keys_are_namespaced_to_avoid_collision() -> None:
    redis = fakeredis.FakeRedis()
    cache = IdempotencyCache(redis, ttl_seconds=60)
    cache.put("token-abc", b"v")
    keys = [k.decode() for k in redis.scan_iter("idem:*")]
    assert keys == ["idem:token-abc"]


def test_distinct_keys_store_distinct_values() -> None:
    cache = _cache()
    cache.put("a", b"1")
    cache.put("b", b"2")
    assert cache.get("a") == b"1"
    assert cache.get("b") == b"2"


def test_overwriting_a_key_updates_the_value() -> None:
    # Not the production path (clients shouldn't reuse the same key for
    # different bodies), but covers the case where put is called twice in
    # rapid succession during dev/test — last-write-wins is the simplest
    # contract and matches Redis SET semantics.
    cache = _cache()
    cache.put("k", b"old")
    cache.put("k", b"new")
    assert cache.get("k") == b"new"


@pytest.mark.parametrize("bad_ttl", [0, -1, -3600])
def test_ttl_must_be_positive(bad_ttl: int) -> None:
    with pytest.raises(ValueError):
        IdempotencyCache(fakeredis.FakeRedis(), ttl_seconds=bad_ttl)
