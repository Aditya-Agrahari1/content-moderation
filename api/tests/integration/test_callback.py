"""Signed provider callbacks: signature checks, idempotency, and the async (> 30 frames) path."""

import json
import os

from app.signing import sign
from tests.helpers.client import API_URL, PROVIDER_URL, Api, identity, video, wait_for

SECRET = os.getenv("PROVIDER_CALLBACK_SECRET", "local-dev-placeholder-not-secret")
LABELS = ("minor", "sexual_violence", "bestiality", "self_harm", "real_person", "explicit")


def full(**scores) -> dict:
    return {label: float(scores.get(label, 0.0)) for label in LABELS}


async def post_callback(api: Api, body: dict | bytes, signature: str | None = "sign"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if signature == "sign":
        headers["X-Signature"] = sign(SECRET, raw)
    elif signature is not None:
        headers["X-Signature"] = signature
    return await api.http.post(f"{API_URL}/provider/callback", content=raw, headers=headers)


async def test_unsigned_and_badly_signed_callbacks_are_401(api: Api):
    body = {"media_id": "00000000-0000-0000-0000-000000000000", "request_id": "r", "frames": []}
    assert (await post_callback(api, body, signature=None)).status_code == 401
    assert (await post_callback(api, body, signature="deadbeef")).status_code == 401
    raw = json.dumps(body).encode()
    tampered = raw.replace(b'"r"', b'"x"')
    assert (await post_callback(api, tampered, signature=sign(SECRET, raw))).status_code == 401
    assert (await post_callback(api, raw, signature="sha256=" + sign("wrong", raw))).status_code == 401


async def test_signed_but_malformed_body_is_422(api: Api):
    assert (await post_callback(api, b"not json")).status_code == 422
    assert (await post_callback(api, {"media_id": "x", "frames": []})).status_code == 422


async def test_unknown_media_is_acknowledged_without_writes(api: Api):
    body = {"media_id": "00000000-0000-0000-0000-000000000001", "request_id": "r", "frames": [full()]}
    assert (await post_callback(api, body)).status_code == 200


async def test_async_video_is_decided_by_callback_and_scored_once(api: Api):
    media_id = await api.upload_ok("member_u", video(40, {39: {"explicit": 0.95}}))

    async def blocked() -> bool:
        return await api.status("member_u", media_id) == "blocked"

    assert await wait_for(blocked, 15), "the last frame of a 40-frame async video must count"
    stats = await api.stats()
    assert stats["calls"][media_id] == 1 and stats["scored"][media_id] == 1


async def test_duplicate_and_conflicting_callbacks_do_not_change_the_decision(api: Api, dbconn):
    media_id = await api.upload_ok("member_v", video(40))

    async def published() -> bool:
        return await api.status("member_v", media_id) == "published"

    assert await wait_for(published, 15)
    replay = {"media_id": media_id, "request_id": "replayed", "frames": [full(minor=0.99)] * 40}
    assert (await post_callback(api, replay)).status_code == 200  # acknowledged, ignored
    assert await api.status("member_v", media_id) == "published"
    assert await dbconn.fetchval("SELECT count(*) FROM screening_results WHERE media_id = $1::uuid", media_id) == 1
    wrong_count = {"media_id": media_id, "request_id": "r", "frames": [full()] * 3}
    assert (await post_callback(api, wrong_count)).status_code == 422


async def test_late_callback_after_admin_reject_changes_nothing(api: Api):
    await api.set_mode("error")
    media_id = await api.upload_ok("member_v", video(40))

    async def held() -> bool:
        return await api.status("member_v", media_id) == "held"

    assert await wait_for(held, 10)
    reject = await api.http.post(f"{API_URL}/review/{media_id}/reject", headers=identity("admin_v"))
    assert reject.status_code == 200
    late = {"media_id": media_id, "request_id": "late", "frames": [full()] * 40}
    assert (await post_callback(api, late)).status_code == 200
    assert await api.status("member_v", media_id) == "blocked"


async def test_provider_replays_async_result_without_rescoring(api: Api):
    """Idempotency-Key replay on the fake provider: 202 again, callback re-sent, no new scoring."""
    media_id = "11111111-2222-3333-4444-555555555555"
    body = {"media_id": media_id, "frames": [{}] * 40, "callback_url": f"{API_URL}/provider/callback"}
    headers = {"Idempotency-Key": media_id}
    first = await api.http.post(f"{PROVIDER_URL}/score", json=body, headers=headers)
    second = await api.http.post(f"{PROVIDER_URL}/score", json=body, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["request_id"] == second.json()["request_id"]
    stats = await api.stats()
    assert stats["calls"][media_id] >= 2 and stats["scored"][media_id] == 1
    conflict = await api.http.post(f"{PROVIDER_URL}/score", json={**body, "frames": [{}] * 41}, headers=headers)
    assert conflict.status_code == 422
