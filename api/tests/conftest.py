import os

import asyncpg
import httpx
import pytest

from tests.helpers.client import Api


@pytest.fixture
async def api():
    async with httpx.AsyncClient(timeout=30) as http:
        client = Api(http)
        await client.set_mode("ok")
        yield client
        await client.set_mode("ok")  # never leave the shared provider broken for the next test


@pytest.fixture
async def dbconn():
    conn = await asyncpg.connect(os.getenv("DATABASE_URL", "postgresql://app:app@db:5432/moderation"))
    yield conn
    await conn.close()
