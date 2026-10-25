"""Redis wake-up list. Only a hint: PostgreSQL holds the jobs, workers fall back to polling."""

import asyncio

import redis.asyncio as redis

from app import log
from app.config import settings

WAKEUP_KEY = "jobs:wakeup"

_client: redis.Redis | None = None


def client() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.from_url(settings.redis_url, socket_timeout=5, socket_connect_timeout=2)
    return _client


async def notify() -> None:
    try:
        await client().lpush(WAKEUP_KEY, 1)
    except redis.RedisError as exc:  # losing a wake-up only delays a job until the next poll
        log.warning("wakeup_notify_failed", error=type(exc).__name__)


async def wait(timeout_s: float) -> None:
    """Block until a wake-up arrives or `timeout_s` passes (BRPOP needs whole seconds)."""
    try:
        await client().brpop([WAKEUP_KEY], timeout=max(1, round(timeout_s)))
    except redis.RedisError:
        await asyncio.sleep(timeout_s)
