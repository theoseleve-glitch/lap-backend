"""T048 (sprint2-backend) — unit tests for the bridge suggest cache.

The cache stores the JSON-serialized response of
GET /api/v1/bridge/suggest_links keyed by source session_id, with
a 1-hour TTL. End-to-end behavior (cache hit vs miss across the
endpoint) is covered in test_bridge_suggest_links_endpoint.py;
this file pins the storage primitives.
"""

from __future__ import annotations

import fakeredis
import pytest
import uuid6

from infra.suggest_cache import (
    DEFAULT_SUGGEST_TTL_SECONDS,
    SuggestCache,
)


def _cache(ttl: int = DEFAULT_SUGGEST_TTL_SECONDS) -> SuggestCache:
    return SuggestCache(fakeredis.FakeRedis(), ttl_seconds=ttl)


def test_get_unknown_session_returns_none() -> None:
    cache = _cache()
    assert cache.get(uuid6.uuid7()) is None


def test_put_then_get_round_trips_bytes() -> None:
    cache = _cache()
    sid = uuid6.uuid7()
    cache.put(sid, b'{"candidates":[]}')
    assert cache.get(sid) == b'{"candidates":[]}'


def test_default_ttl_is_one_hour() -> None:
    """Sprint mandate: 1h TTL. Pin the constant so any future tweak
    trips a test (and re-prompts the staleness-budget conversation)."""
    assert DEFAULT_SUGGEST_TTL_SECONDS == 3600


def test_put_sets_configured_ttl_on_redis_key() -> None:
    redis = fakeredis.FakeRedis()
    cache = SuggestCache(redis, ttl_seconds=3600)
    sid = uuid6.uuid7()
    cache.put(sid, b"x")
    ttl = redis.ttl(f"bridge_suggest:{sid}")
    # Small slack: the call is not instantaneous.
    assert 3590 <= ttl <= 3600


def test_keys_are_namespaced_to_bridge_suggest() -> None:
    """Prefix prevents collisions with idempotency / pairing keys
    in shared Redis."""
    redis = fakeredis.FakeRedis()
    cache = SuggestCache(redis, ttl_seconds=60)
    sid = uuid6.uuid7()
    cache.put(sid, b"v")
    keys = [k.decode() for k in redis.scan_iter("bridge_suggest:*")]
    assert keys == [f"bridge_suggest:{sid}"]


def test_distinct_sessions_store_distinct_values() -> None:
    """Per-session isolation: two sources, two cache entries."""
    cache = _cache()
    a = uuid6.uuid7()
    b = uuid6.uuid7()
    cache.put(a, b"1")
    cache.put(b, b"2")
    assert cache.get(a) == b"1"
    assert cache.get(b) == b"2"


def test_overwriting_a_session_updates_value() -> None:
    """Overwrite within TTL is last-write-wins — matches Redis SET
    semantics. Endpoint never overwrites in normal flow (cache
    expires naturally), but the contract is pinned."""
    cache = _cache()
    sid = uuid6.uuid7()
    cache.put(sid, b"old")
    cache.put(sid, b"new")
    assert cache.get(sid) == b"new"


@pytest.mark.parametrize("bad_ttl", [0, -1, -3600])
def test_ttl_must_be_positive(bad_ttl: int) -> None:
    with pytest.raises(ValueError):
        SuggestCache(fakeredis.FakeRedis(), ttl_seconds=bad_ttl)
