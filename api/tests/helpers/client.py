"""Black-box helpers shared by tests and scripts. They use only the HTTP API, the provider's
/_control and /_stats, and database columns that exist at both `broken-baseline` and the fix."""

import asyncio
import os
import time
from collections.abc import Awaitable, Callable

import httpx

API_URL = os.getenv("TEST_API_URL", "http://localhost:8000")
PROVIDER_URL = os.getenv("PROVIDER_URL", "http://provider:8001")

USERS = {
    "admin_v": "ws_verified",
    "member_v": "ws_verified",
    "admin_u": "ws_unverified",
    "member_u": "ws_unverified",
}


def identity(user: str) -> dict[str, str]:
    return {"X-User-Id": user, "X-Workspace-Id": USERS[user]}


def image(**scores: float) -> dict:
    return {"type": "image", "source": "upload", "frames": [dict(scores)]}


def video(n_frames: int, bad_frames: dict[int, dict[str, float]] | None = None, base: float = 0.01) -> dict:
    frames = [{"explicit": base} for _ in range(n_frames)]
    for index, scores in (bad_frames or {}).items():
        frames[index] = dict(scores)
    return {"type": "video", "source": "upload", "frames": frames}


class Api:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def upload(self, user: str, body: dict) -> httpx.Response:
        return await self.http.post(f"{API_URL}/media", json=body, headers=identity(user))

    async def upload_ok(self, user: str, body: dict) -> str:
        response = await self.upload(user, body)
        assert response.status_code == 202, response.text
        return response.json()["id"]

    async def media(self, user: str, media_id: str) -> httpx.Response:
        return await self.http.get(f"{API_URL}/media/{media_id}", headers=identity(user))

    async def status(self, user: str, media_id: str) -> str:
        response = await self.media(user, media_id)
        assert response.status_code == 200, response.text
        return response.json()["status"]

    async def set_mode(self, mode: str, delay_ms: int | None = None) -> None:
        body = {"mode": mode} if delay_ms is None else {"mode": mode, "delay_ms": delay_ms}
        response = await self.http.post(f"{PROVIDER_URL}/_control", json=body)
        assert response.status_code == 200, response.text

    async def stats(self) -> dict:
        return (await self.http.get(f"{PROVIDER_URL}/_stats")).json()

    async def calls(self, media_id: str) -> int:
        return (await self.stats())["calls"].get(media_id, 0)

    async def scored(self, media_id: str) -> int:
        return (await self.stats()).get("scored", {}).get(media_id, 0)


async def wait_for(condition: Callable[[], Awaitable[bool]], timeout_s: float, interval_s: float = 0.2) -> bool:
    """Poll until `condition()` is true. Returns False on timeout instead of raising."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await condition():
            return True
        await asyncio.sleep(interval_s)
    return await condition()
