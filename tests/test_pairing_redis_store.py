import uuid6

from infra.pairing_store import RedisPairingStore


def _store() -> RedisPairingStore:
    import fakeredis  # local import keeps test-only dep out of prod hot paths

    return RedisPairingStore(fakeredis.FakeRedis(), ttl_seconds=300)


def test_redis_store_create_returns_token() -> None:
    store = _store()
    user_id = uuid6.uuid7()
    token = store.create(user_id)
    assert isinstance(token, str)
    assert len(token) >= 32


def test_redis_store_consume_returns_user_id() -> None:
    store = _store()
    user_id = uuid6.uuid7()
    token = store.create(user_id)
    consumed = store.consume(token)
    assert consumed == user_id


def test_redis_store_consume_unknown_returns_none() -> None:
    store = _store()
    assert store.consume("not-a-real-token-xxxxxxxxxxxxxxxx") is None


def test_redis_store_consume_is_single_use() -> None:
    store = _store()
    token = store.create(uuid6.uuid7())
    first = store.consume(token)
    second = store.consume(token)
    assert first is not None
    assert second is None


def test_redis_store_create_sets_ttl_to_300_seconds() -> None:
    import fakeredis

    redis = fakeredis.FakeRedis()
    store = RedisPairingStore(redis, ttl_seconds=300)
    token = store.create(uuid6.uuid7())
    ttl = redis.ttl(f"pairing:{token}")
    assert 290 <= ttl <= 300


def test_redis_store_expired_token_returns_none() -> None:
    import fakeredis

    redis = fakeredis.FakeRedis()
    store = RedisPairingStore(redis, ttl_seconds=300)
    token = store.create(uuid6.uuid7())
    # Simulate expiry by deleting the key directly — equivalent to TTL elapsing.
    redis.delete(f"pairing:{token}")
    assert store.consume(token) is None


def test_redis_store_distinct_tokens_for_distinct_calls() -> None:
    store = _store()
    t1 = store.create(uuid6.uuid7())
    t2 = store.create(uuid6.uuid7())
    assert t1 != t2


def test_redis_store_uses_namespaced_key() -> None:
    import fakeredis

    redis = fakeredis.FakeRedis()
    store = RedisPairingStore(redis, ttl_seconds=300)
    token = store.create(uuid6.uuid7())
    # Confirm we don't pollute the keyspace — pairing tokens should sit under a
    # dedicated prefix so other Redis-using features don't collide.
    keys = list(redis.scan_iter("pairing:*"))
    assert len(keys) == 1
    assert keys[0].decode() == f"pairing:{token}"
