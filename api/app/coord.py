"""Shared, short-lived coordination state (circuit breaker, 429 pause, counters).

Kept in Redis so all workers agree. If Redis is unreachable each process falls back to its own
in-memory copy: workers then disagree a little (more probe calls) but every decision stays safe,
because nothing here is a source of truth for media or jobs.
"""

import time

import redis.asyncio as redis

from app import log, wakeup


class Store:
    def __init__(self, client: redis.Redis | None = None) -> None:
        self._client = client
        self._local: dict[str, tuple[str, float | None]] = {}  # key -> (value, expires_at)

    def _redis(self) -> redis.Redis | None:
        return self._client

    def _local_get(self, key: str) -> str | None:
        value = self._local.get(key)
        if value is None:
            return None
        if value[1] is not None and value[1] <= time.time():
            del self._local[key]
            return None
        return value[0]

    async def get(self, key: str) -> str | None:
        client = self._redis()
        if client is not None:
            try:
                raw = await client.get(key)
                return raw.decode() if isinstance(raw, bytes) else raw
            except redis.RedisError as exc:
                log.warning("coord_fallback", op="get", error=type(exc).__name__)
        return self._local_get(key)

    async def set(self, key: str, value: str, ttl_s: float | None = None, only_if_absent: bool = False) -> bool:
        client = self._redis()
        if client is not None:
            try:
                px = int(ttl_s * 1000) if ttl_s else None
                return bool(await client.set(key, value, px=px, nx=only_if_absent))
            except redis.RedisError as exc:
                log.warning("coord_fallback", op="set", error=type(exc).__name__)
        if only_if_absent and self._local_get(key) is not None:
            return False
        self._local[key] = (value, time.time() + ttl_s if ttl_s else None)
        return True

    async def delete(self, *keys: str) -> None:
        client = self._redis()
        if client is not None:
            try:
                await client.delete(*keys)
                return
            except redis.RedisError as exc:
                log.warning("coord_fallback", op="delete", error=type(exc).__name__)
        for key in keys:
            self._local.pop(key, None)

    async def incr(self, key: str, ttl_s: float | None = None) -> int:
        client = self._redis()
        if client is not None:
            try:
                value = await client.incr(key)
                if ttl_s:
                    await client.pexpire(key, int(ttl_s * 1000))
                return int(value)
            except redis.RedisError as exc:
                log.warning("coord_fallback", op="incr", error=type(exc).__name__)
        value = int(self._local_get(key) or 0) + 1
        self._local[key] = (str(value), time.time() + ttl_s if ttl_s else None)
        return value


_store: Store | None = None


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(wakeup.client())
    return _store


METRIC_KEYS = ("callback_rejected", "callback_duplicate", "stale_discarded", "lease_expirations",
               "retries_scheduled", "budget_exhausted", "circuit_skips", "probe_requeued", "recovery_requeued")


async def count(name: str) -> None:
    """Best-effort operational counter; the structured logs remain the record."""
    await store().incr(f"metrics:{name}")


async def counters() -> dict[str, int]:
    return {name: int(await store().get(f"metrics:{name}") or 0) for name in METRIC_KEYS}
