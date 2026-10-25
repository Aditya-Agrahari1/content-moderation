"""PostgreSQL access with asyncpg. Every write path uses an explicit `conn.transaction()`."""

import json

import asyncpg

from app.config import settings

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    # JSONB parameters and results are plain Python objects.
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def open_pool(min_size: int = 1, max_size: int = 10) -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            settings.database_url, min_size=min_size, max_size=max_size, init=_init_connection, command_timeout=30
        )
    return _pool


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool is not open")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
