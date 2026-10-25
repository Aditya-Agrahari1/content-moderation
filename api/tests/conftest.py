import os

import asyncpg
import httpx
import pytest

from tests.helpers.client import Api

COORD_KEYS = ("cb:failures", "cb:open_until", "cb:cooldown_s", "cb:canary", "provider:pause_until")


async def _reset_circuit() -> None:
    """Tests share one provider and one circuit breaker; start each test from a closed circuit.
    (Only the fixed version has a breaker; on broken-baseline these keys simply do not exist.)"""
    try:
        import redis.asyncio as redis

        client = redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
        await client.delete(*COORD_KEYS)
        await client.aclose()
    except Exception:
        pass


@pytest.fixture
async def api():
    async with httpx.AsyncClient(timeout=30) as http:
        client = Api(http)
        await client.set_mode("ok")
        await _reset_circuit()
        yield client
        await client.set_mode("ok")  # never leave the shared provider broken for the next test
        await _reset_circuit()


@pytest.fixture
async def dbconn():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL", "postgresql://app:app@db:5432/moderation"))
    await conn.set_type_codec("jsonb", encoder=__import__("json").dumps, decoder=__import__("json").loads,
                              schema="pg_catalog")
    yield conn
    await conn.close()
