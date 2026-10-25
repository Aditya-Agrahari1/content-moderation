import time

from app.circuit import Circuit
from app.config import settings
from app.coord import Store


def circuit() -> Circuit:
    return Circuit(Store(client=None))  # in-memory store: same code path as the Redis-down fallback


async def test_opens_after_threshold_consecutive_failures():
    c = circuit()
    for _ in range(settings.cb_failure_threshold - 1):
        await c.record_failure(is_canary=False)
    assert await c.state() == "closed"
    await c.record_failure(is_canary=False)
    assert await c.state() == "open"
    assert await c.allow_call() == (False, False)


async def test_success_resets_the_failure_streak():
    c = circuit()
    for _ in range(settings.cb_failure_threshold - 1):
        await c.record_failure(is_canary=False)
    await c.record_success(is_canary=False)
    await c.record_failure(is_canary=False)
    assert await c.state() == "closed"


async def test_half_open_allows_exactly_one_canary():
    c = circuit()
    await c.store.set("cb:open_until", str(time.time() - 1))
    assert await c.state() == "half_open"
    assert await c.allow_call() == (True, True)
    assert await c.allow_call() == (False, False)  # second worker keeps skipping


async def test_canary_failure_reopens_with_doubled_cooldown():
    c = circuit()
    await c.store.set("cb:open_until", str(time.time() - 1))
    await c.store.set("cb:cooldown_s", "2")
    _, canary = await c.allow_call()
    await c.record_failure(canary)
    assert await c.state() == "open"
    assert float(await c.store.get("cb:cooldown_s")) == min(4.0, settings.cb_cooldown_max_s)
    assert await c.store.get("cb:canary") is None


async def test_canary_success_closes():
    c = circuit()
    await c.store.set("cb:open_until", str(time.time() - 1))
    _, canary = await c.allow_call()
    await c.record_success(canary)
    assert await c.state() == "closed"
    assert await c.allow_call() == (True, False)


async def test_pause_for_429_is_separate_from_the_circuit():
    c = circuit()
    await c.pause(5)
    assert await c.paused_until() is not None
    assert await c.state() == "closed"
