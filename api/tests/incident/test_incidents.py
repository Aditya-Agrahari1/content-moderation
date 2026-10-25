"""Incident regression tests (black-box). The same file runs against `broken-baseline` and the fix.

They assert on media status (via the API) and provider evidence (/_stats), never on job status:
a finished job is not proof that the media was screened.

Run against the stack started with docker-compose.test.yml (provider read timeout 3 s).
"""

from tests.helpers.client import Api, image, video, wait_for

SETTLE_S = 20


async def _left_pending(api: Api, user: str, media_id: str) -> bool:
    return await api.status(user, media_id) != "pending"


async def test_timeout_does_not_publish(api: Api):
    """Bug 1: a provider timeout was treated as a pass and the upload went live unchecked."""
    await api.set_mode("timeout")
    media_id = await api.upload_ok("member_u", image(explicit=0.01))

    await wait_for(lambda: _left_pending(api, "member_u", media_id), SETTLE_S)
    status = await api.status("member_u", media_id)
    assert status != "published", "published although the provider never returned a decision"
    assert status == "held", f"media must be held while no decision can be obtained, got {status}"
    assert await api.scored(media_id) == 0  # sanity: the provider really scored nothing


async def test_late_frame_is_screened(api: Api):
    """Bug 2: only the first 8 frames were screened, so late explicit/prohibited frames went live."""
    unverified_id = await api.upload_ok("member_u", video(20, {19: {"explicit": 0.95}}))
    verified_id = await api.upload_ok("member_v", video(20, {15: {"minor": 0.95}}))

    assert await wait_for(lambda: _left_pending(api, "member_u", unverified_id), SETTLE_S)
    assert await wait_for(lambda: _left_pending(api, "member_v", verified_id), SETTLE_S)
    assert await api.status("member_u", unverified_id) == "blocked", "explicit last frame, unverified workspace"
    assert await api.status("member_v", verified_id) == "blocked", "minor at frame 15 blocks in every workspace"


async def test_prohibited_prompt_refused(api: Api):
    """Bug 3: a generation prompt asking for a 16-year-old reached the generator."""
    before = await api.stats()
    body = {"type": "image", "source": "generation", "prompt": "explicit photo of a 16-year-old girl",
            "frames": [{"explicit": 0.1}]}
    response = await api.upload("member_v", body)

    assert response.status_code == 422, f"expected refusal, got {response.status_code} {response.text}"
    assert response.json().get("reasons"), "a refusal must say why"
    assert (await api.stats())["calls"] == before["calls"], "nothing may be sent for scoring"


async def test_redelivery_while_running(api: Api, dbconn):
    """Bug 4: a redelivered job ran while the first attempt was still calling the provider.

    The harness simulates redelivery by putting the SAME job row back to 'queued' while the original
    provider call is still in flight (slow mode). Only shared columns are touched.
    """
    # Slow, but below the 3 s client read timeout: the original call succeeds if left alone.
    await api.set_mode("slow", delay_ms=1500)
    media_id = await api.upload_ok("member_v", image(explicit=0.01))

    async def in_flight() -> bool:
        return await api.calls(media_id) >= 1

    assert await wait_for(in_flight, 10), "provider call never started"
    await dbconn.execute("UPDATE screening_jobs SET status = 'queued' WHERE media_id = $1::uuid", media_id)

    async def decided() -> bool:
        return await api.status("member_v", media_id) in ("published", "blocked")

    assert await wait_for(decided, SETTLE_S)
    await wait_for(lambda: _calls_above(api, media_id, 1), 5)  # give a duplicate attempt time to show up
    assert await api.calls(media_id) == 1, "a redelivered job made a second concurrent provider call"
    assert await api.scored(media_id) == 1, "the media was scored more than once"
    assert await api.status("member_v", media_id) == "published"


async def _calls_above(api: Api, media_id: str, n: int) -> bool:
    return await api.calls(media_id) > n
