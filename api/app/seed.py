"""Idempotent seed of the workspaces and users required by the assignment."""

import asyncio

from app import db

WORKSPACES = [("ws_verified", True), ("ws_unverified", False)]
USERS = [
    ("admin_v", "ws_verified", "admin"),
    ("member_v", "ws_verified", "member"),
    ("admin_u", "ws_unverified", "admin"),
    ("member_u", "ws_unverified", "member"),
]


async def seed() -> None:
    pool = await db.open_pool(max_size=1)
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(
            "INSERT INTO workspaces (id, verified) VALUES ($1, $2) "
            "ON CONFLICT (id) DO UPDATE SET verified = EXCLUDED.verified",
            WORKSPACES,
        )
        await conn.executemany(
            "INSERT INTO users (id, workspace_id, role) VALUES ($1, $2, $3) "
            "ON CONFLICT (id) DO UPDATE SET workspace_id = EXCLUDED.workspace_id, role = EXCLUDED.role",
            USERS,
        )
    await db.close_pool()
    print("seed: ok")


if __name__ == "__main__":
    asyncio.run(seed())
