"""Unit tests for the in-memory job event bus (T113, T114)."""

import asyncio

import pytest

from infra.job_event_bus import InMemoryJobEventBus, subscription


async def test_subscribe_yields_previously_published_events() -> None:
    bus = InMemoryJobEventBus()
    bus.publish("job-1", "queued", {"x": 1})
    bus.publish("job-1", "processing", {"x": 2})

    queue = bus.subscribe("job-1")
    e1 = await queue.get()
    e2 = await queue.get()
    assert e1 == (1, "queued", {"x": 1})
    assert e2 == (2, "processing", {"x": 2})


async def test_subscribe_yields_only_events_for_subscribed_job() -> None:
    bus = InMemoryJobEventBus()
    bus.publish("job-1", "queued", {})
    bus.publish("job-2", "queued", {})
    bus.publish("job-1", "processing", {})

    queue = bus.subscribe("job-1")
    received: list[str] = []
    while not queue.empty():
        _, name, _ = queue.get_nowait()
        received.append(name)
    assert received == ["queued", "processing"]


async def test_subscribe_streams_events_published_after_subscription() -> None:
    bus = InMemoryJobEventBus()
    queue = bus.subscribe("job-1")
    bus.publish("job-1", "queued", {})
    bus.publish("job-1", "processing", {})
    bus.publish("job-1", "completed", {})

    received: list[str] = []
    for _ in range(3):
        _, name, _ = await asyncio.wait_for(queue.get(), timeout=2.0)
        received.append(name)
    assert received == ["queued", "processing", "completed"]


async def test_multiple_subscribers_each_receive_all_events() -> None:
    bus = InMemoryJobEventBus()
    bus.publish("job-1", "queued", {})
    bus.publish("job-1", "completed", {})

    qa = bus.subscribe("job-1")
    qb = bus.subscribe("job-1")
    drained_a = [qa.get_nowait()[1], qa.get_nowait()[1]]
    drained_b = [qb.get_nowait()[1], qb.get_nowait()[1]]
    assert drained_a == drained_b == ["queued", "completed"]


async def test_unsubscribe_stops_receiving_new_events() -> None:
    bus = InMemoryJobEventBus()
    queue = bus.subscribe("job-1")
    bus.publish("job-1", "queued", {})
    while not queue.empty():
        queue.get_nowait()

    bus.unsubscribe("job-1", queue)
    bus.publish("job-1", "completed", {})
    assert queue.empty()


async def test_subscription_context_unsubscribes_on_exit() -> None:
    bus = InMemoryJobEventBus()
    bus.publish("job-1", "queued", {})
    with subscription(bus, "job-1") as queue:
        assert (await asyncio.wait_for(queue.get(), 1)) == (1, "queued", {})
    bus.publish("job-1", "completed", {})
    assert queue.empty()


async def test_publish_assigns_monotonic_ids_per_job() -> None:
    bus = InMemoryJobEventBus()
    bus.publish("job-1", "a", {})
    bus.publish("job-1", "b", {})
    bus.publish("job-1", "c", {})
    queue = bus.subscribe("job-1")
    ids = [queue.get_nowait()[0] for _ in range(3)]
    assert ids == [1, 2, 3]


async def test_id_counters_are_independent_across_jobs() -> None:
    bus = InMemoryJobEventBus()
    bus.publish("job-1", "a", {})
    bus.publish("job-2", "a", {})
    bus.publish("job-1", "b", {})

    q1 = bus.subscribe("job-1")
    q2 = bus.subscribe("job-2")
    assert q1.get_nowait()[0] == 1
    assert q1.get_nowait()[0] == 2
    assert q2.get_nowait()[0] == 1


async def test_subscribe_with_after_id_filters_replay() -> None:
    bus = InMemoryJobEventBus()
    bus.publish("job-1", "a", {})  # id=1
    bus.publish("job-1", "b", {})  # id=2
    bus.publish("job-1", "c", {})  # id=3

    queue = bus.subscribe("job-1", after_id=1)
    drained: list[str] = []
    while not queue.empty():
        _, name, _ = queue.get_nowait()
        drained.append(name)
    assert drained == ["b", "c"]


async def test_subscribe_with_after_id_beyond_buffer_is_empty() -> None:
    bus = InMemoryJobEventBus()
    bus.publish("job-1", "a", {})
    queue = bus.subscribe("job-1", after_id=99)
    assert queue.empty()


@pytest.mark.parametrize("bad_event", ["", "  "])
def test_publish_rejects_empty_event_name(bad_event: str) -> None:
    bus = InMemoryJobEventBus()
    with pytest.raises(ValueError):
        bus.publish("job-1", bad_event, {})
