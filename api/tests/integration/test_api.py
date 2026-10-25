"""Contract, isolation, authorization and review semantics against the running stack."""

import uuid

from tests.helpers.client import API_URL, Api, identity, image, video, wait_for


async def decided(api: Api, user: str, media_id: str, timeout: float = 15) -> str:
    async def done() -> bool:
        return await api.status(user, media_id) in ("published", "blocked") or (
            (await api.media(user, media_id)).json()["reasons"] not in ([], ["provider_unavailable"])
        )

    await wait_for(done, timeout)
    return await api.status(user, media_id)


async def test_contract_shapes(api: Api):
    response = await api.upload("member_v", image(explicit=0.1))
    assert response.status_code == 202
    body = response.json()
    assert set(body) == {"id", "status"} and body["status"] == "pending"
    assert await decided(api, "member_v", body["id"]) == "published"
    got = (await api.media("member_v", body["id"])).json()
    assert set(got) == {"id", "status", "reasons"} and got["reasons"] == []


async def test_any_member_of_the_owning_workspace_can_read(api: Api):
    media_id = await api.upload_ok("admin_v", image(explicit=0.1))
    assert (await api.media("member_v", media_id)).status_code == 200


async def test_cross_workspace_access_is_404(api: Api):
    media_id = await api.upload_ok("member_v", image(self_harm=0.6))
    assert await decided(api, "member_v", media_id) == "held"
    assert (await api.media("admin_u", media_id)).status_code == 404
    assert (await api.media("member_u", media_id)).status_code == 404
    for action in ("approve", "reject"):
        response = await api.http.post(f"{API_URL}/review/{media_id}/{action}", headers=identity("admin_u"))
        assert response.status_code == 404
    other_queue = (await api.http.get(f"{API_URL}/review", headers=identity("admin_u"))).json()
    assert media_id not in {item["id"] for item in other_queue}
    assert (await api.media("member_v", str(uuid.uuid4()))).status_code == 404
    assert (await api.media("member_v", "not-a-uuid")).status_code == 404


async def test_identity_errors(api: Api):
    assert (await api.http.get(f"{API_URL}/media/{uuid.uuid4()}")).status_code == 401
    assert (await api.http.post(f"{API_URL}/media", json=image())).status_code == 401
    mismatch = {"X-User-Id": "member_v", "X-Workspace-Id": "ws_unverified"}
    assert (await api.http.post(f"{API_URL}/media", json=image(), headers=mismatch)).status_code == 403
    unknown = {"X-User-Id": "mallory", "X-Workspace-Id": "ws_verified"}
    assert (await api.http.get(f"{API_URL}/review", headers=unknown)).status_code == 403


async def test_member_cannot_review(api: Api):
    media_id = await api.upload_ok("member_v", image(minor=0.6))
    assert await decided(api, "member_v", media_id) == "held"
    assert (await api.http.get(f"{API_URL}/review", headers=identity("member_v"))).status_code == 403
    for action in ("approve", "reject"):
        response = await api.http.post(f"{API_URL}/review/{media_id}/{action}", headers=identity("member_v"))
        assert response.status_code == 403
    assert await api.status("member_v", media_id) == "held"


async def test_review_band_is_held_and_admin_can_approve_once(api: Api):
    media_id = await api.upload_ok("member_v", image(self_harm=0.6))
    assert await decided(api, "member_v", media_id) == "held"
    queue = (await api.http.get(f"{API_URL}/review", headers=identity("admin_v"))).json()
    item = next(i for i in queue if i["id"] == media_id)
    assert item["hold_reason"] == "needs_human_review" and item["reviewable"] is True
    assert item["reasons"] == ["self_harm_review"]
    approve = await api.http.post(f"{API_URL}/review/{media_id}/approve", headers=identity("admin_v"))
    assert approve.status_code == 200 and approve.json() == {"id": media_id, "status": "published"}
    again = await api.http.post(f"{API_URL}/review/{media_id}/reject", headers=identity("admin_v"))
    assert again.status_code == 409


async def test_explicit_rules_per_workspace(api: Api):
    verified = await api.upload_ok("member_v", image(explicit=0.95))
    unverified = await api.upload_ok("member_u", image(explicit=0.95))
    assert await decided(api, "member_v", verified) == "published"
    assert await decided(api, "member_u", unverified) == "blocked"
    assert (await api.media("member_u", unverified)).json()["reasons"] == ["explicit_unverified"]


async def test_prohibited_blocks_even_in_verified_workspace(api: Api):
    for label in ("minor", "sexual_violence", "bestiality", "self_harm", "real_person"):
        media_id = await api.upload_ok("member_v", video(5, {4: {label: 0.8}}))
        assert await decided(api, "member_v", media_id) == "blocked", label


async def test_invalid_input_returns_reasons(api: Api):
    for body in (
        {"type": "image", "source": "upload", "frames": [{"nudity": 0.5}]},
        {"type": "image", "source": "upload", "frames": [{"explicit": True}]},
        {"type": "image", "source": "upload", "frames": [{"explicit": 2}]},
        {"type": "image", "source": "upload", "frames": [{}, {}]},
        {"type": "image", "source": "generation", "frames": [{}]},
    ):
        response = await api.upload("member_v", body)
        assert response.status_code == 422 and response.json()["reasons"], body


async def test_outage_hold_is_visible_but_not_approvable(api: Api):
    await api.set_mode("error")
    media_id = await api.upload_ok("member_u", image(explicit=0.01))

    async def held() -> bool:
        return await api.status("member_u", media_id) == "held"

    assert await wait_for(held, 10)
    queue = (await api.http.get(f"{API_URL}/review", headers=identity("admin_u"))).json()
    item = next(i for i in queue if i["id"] == media_id)
    assert item["hold_reason"] == "provider_unavailable" and item["reviewable"] is False
    approve = await api.http.post(f"{API_URL}/review/{media_id}/approve", headers=identity("admin_u"))
    assert approve.status_code == 409 and approve.json()["detail"] == "not_screened"
    reject = await api.http.post(f"{API_URL}/review/{media_id}/reject", headers=identity("admin_u"))
    assert reject.status_code == 200 and reject.json()["status"] == "blocked"
    calls_after_reject = await api.calls(media_id)
    await api.set_mode("ok")
    await wait_for(lambda: _calls_above(api, media_id, calls_after_reject), 6)
    assert await api.calls(media_id) == calls_after_reject, "a rejected item must not be re-screened"
    assert await api.status("member_u", media_id) == "blocked"


async def _calls_above(api: Api, media_id: str, n: int) -> bool:
    return await api.calls(media_id) > n
