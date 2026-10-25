"""Queue recovery, retries, fencing, crash windows and fairness against the running stack
(docker-compose.test.yml: read timeout 3 s, lease 6 s, cap 2 per workspace, cooldown 2-8 s)."""

import asyncio
import time

import httpx
import pytest

from app import coord
from app.circuit import Circuit
from app.worker import process as worker_process
from tests.helpers.client import API_URL, Api, image, video, wait_for


async def status_is(api: Api, user: str, media_id: str, *wanted: str) -> bool:
    return await api.status(user, media_id) in wanted


async def job(dbconn, media_id: str):
    return await dbconn.fetchrow("SELECT * FROM screening_jobs WHERE media_id = $1::uuid", media_id)


async def test_outage_then_recovery_rescreens_without_a_human(api: Api, dbconn):
    await api.set_mode("error")
    ids = [await api.upload_ok("member_u", image(explicit=0.01)) for _ in range(3)]
    for media_id in ids:
        assert await wait_for(lambda m=media_id: status_is(api, "member_u", m, "held"), 10)
    # Let the outage outlast a per-cycle budget so at least one job reaches held_provider.
    await wait_for(lambda: _any_held_provider(dbconn, ids), 20)
    await api.set_mode("ok")
    for media_id in ids:
        assert await wait_for(lambda m=media_id: status_is(api, "member_u", m, "published"), 40), media_id
    # Every ID the provider ever saw is a real media item: probes used held jobs, never fake IDs.
    seen = list((await api.stats())["calls"])
    known = await dbconn.fetchval("SELECT count(*) FROM media WHERE id::text = ANY($1::text[])", seen)
    synthetic = [i for i in seen if not i.startswith("11111111-")]  # provider replay test uses a fixed ID
    assert known == len(synthetic)


async def _any_held_provider(dbconn, ids) -> bool:
    return await dbconn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM screening_jobs WHERE media_id = ANY($1::uuid[]) AND status = 'held_provider')", ids
    )


async def test_probe_reuses_an_existing_held_job(api: Api, dbconn):
    """The Revision-1 deadlock: every job exhausted, circuit open, nothing queued to probe with.
    The sweeper must re-queue an existing held_provider job as the canary."""
    await api.set_mode("error")
    media_id = await api.upload_ok("member_u", image())
    assert await wait_for(lambda: status_is(api, "member_u", media_id, "held"), 10)
    # Simulate an exhausted budget during a long outage: held_provider, circuit open but cooled down.
    await dbconn.execute(
        "UPDATE screening_jobs SET status = 'held_provider', lease_expires = NULL, next_retry_at = NULL, "
        "cycle_failures = 4 WHERE media_id = $1::uuid", media_id)
    store = coord.store()
    await store.set("cb:open_until", str(time.time() - 1))
    await store.set("cb:cooldown_s", "2")
    before = await coord.counters()
    cycle_before = (await job(dbconn, media_id))["cycle"]
    await api.set_mode("ok")
    assert await wait_for(lambda: status_is(api, "member_u", media_id, "published"), 30)
    after = await coord.counters()
    requeued = (after["probe_requeued"] - before["probe_requeued"]) + (after["recovery_requeued"] - before["recovery_requeued"])
    assert requeued >= 1, "recovered by the sweeper, not by a human"
    assert (await job(dbconn, media_id))["cycle"] > cycle_before, "re-queue starts a new cycle (fresh budget)"


async def test_circuit_open_makes_no_calls(api: Api, dbconn):
    await api.set_mode("error")
    warm = [await api.upload_ok("member_v", image()) for _ in range(4)]  # trip the breaker (threshold 3)
    for media_id in warm:
        await wait_for(lambda m=media_id: status_is(api, "member_v", m, "held"), 10)
    assert await wait_for(lambda: _circuit_open(), 10)
    media_id = await api.upload_ok("member_u", image())
    assert await wait_for(lambda: status_is(api, "member_u", media_id, "held"), 5), "held while the circuit is open"
    assert await api.calls(media_id) == 0, "no provider call while the circuit is open"
    assert (await job(dbconn, media_id))["attempt"] == 0, "a skip does not use an attempt"


async def _circuit_open() -> bool:
    return await Circuit(coord.store()).state() == "open"


async def test_stale_worker_cannot_overwrite(api: Api, dbconn):
    """Worker A's lease expires mid-call; B re-claims (new token). A's late result is discarded."""
    await api.set_mode("slow", delay_ms=2000)
    media_id = await api.upload_ok("member_v", image())
    assert await wait_for(lambda: _calls_at_least(api, media_id, 1), 10)
    await dbconn.execute(
        "UPDATE screening_jobs SET status = 'queued', lease_expires = now() - interval '1 second' "
        "WHERE media_id = $1::uuid", media_id)
    assert await wait_for(lambda: status_is(api, "member_v", media_id, "published"), 20)
    await asyncio.sleep(2.5)  # let both workers finish
    attempts = await dbconn.fetch(
        "SELECT a.lease_token FROM job_attempts a JOIN screening_jobs j ON j.id = a.job_id WHERE j.media_id = $1::uuid",
        media_id)
    assert len({a["lease_token"] for a in attempts}) == 2, "two claims, two different fencing tokens"
    assert await dbconn.fetchval("SELECT count(*) FROM screening_results WHERE media_id = $1::uuid", media_id) == 1
    stats = await api.stats()
    assert stats["calls"][media_id] == 2 and stats["scored"][media_id] == 1  # second call answered by replay


async def _calls_at_least(api: Api, media_id: str, n: int) -> bool:
    return await api.calls(media_id) >= n


async def test_workspace_cap_holds_and_other_workspace_is_not_starved(api: Api, dbconn):
    await api.set_mode("slow", delay_ms=1500)
    burst = [await api.upload_ok("member_v", image()) for _ in range(10)]
    peak, deadline = 0, time.monotonic() + 3
    while time.monotonic() < deadline:
        live = await dbconn.fetchval(
            "SELECT count(*) FROM screening_jobs WHERE workspace_id = 'ws_verified' AND lease_expires > now() "
            "AND status IN ('claimed', 'queued') AND media_id = ANY($1::uuid[])", burst)
        peak = max(peak, live)
        await asyncio.sleep(0.05)
    started = time.monotonic()
    other = await api.upload_ok("member_u", image())
    assert await wait_for(lambda: _calls_at_least(api, other, 1), 5), "ws_unverified starved by the burst"
    waited = time.monotonic() - started
    assert peak <= 2, f"cap is 2 live leases per workspace, saw {peak}"
    assert waited < 2.5, f"other workspace waited {waited:.1f}s for a worker"
    for media_id in burst:
        assert await wait_for(lambda m=media_id: status_is(api, "member_v", m, "published"), 30)


async def test_callback_watchdog_recovers_a_lost_callback(api: Api, dbconn):
    """A job waiting for a callback that never comes is held, then re-requested; scored once."""
    media_id = await dbconn.fetchval(
        "INSERT INTO media (workspace_id, user_id, type, source, frames) "
        "VALUES ('ws_verified', 'member_v', 'video', 'upload', $1) RETURNING id::text", [{}] * 40)
    await dbconn.execute(
        "INSERT INTO screening_jobs (media_id, workspace_id, status, callback_deadline) "
        "VALUES ($1::uuid, 'ws_verified', 'waiting_callback', now() - interval '1 second')", media_id)
    assert await wait_for(lambda: _job_status(dbconn, media_id, "held_provider"), 5), "watchdog did not fire"
    assert await api.status("member_v", media_id) == "held"
    assert await wait_for(lambda: status_is(api, "member_v", media_id, "published"), 20)
    assert (await job(dbconn, media_id))["status"] == "completed"


async def _job_status(dbconn, media_id: str, wanted: str) -> bool:
    return (await job(dbconn, media_id))["status"] == wanted


async def test_crash_after_scoring_before_saving(api: Api, dbconn, monkeypatch):
    """Fault injection at the exact point: the provider answered, the worker dies before the commit.
    Recovery: the lease expires, the job is re-claimed, the provider replays the stored result."""
    media_id = await dbconn.fetchval(
        "INSERT INTO media (workspace_id, user_id, type, source, frames) "
        "VALUES ('ws_verified', 'member_v', 'image', 'upload', $1) RETURNING id::text", [{"explicit": 0.1}])
    # Park the job far in the future so the running workers leave it to this test, then claim it here.
    claimed = await dbconn.fetchrow(
        "INSERT INTO screening_jobs (media_id, workspace_id, status, lease_token, lease_expires, next_retry_at) "
        "VALUES ($1::uuid, 'ws_verified', 'claimed', gen_random_uuid(), now() + interval '4 seconds', "
        "now() + interval '1 hour') RETURNING *", media_id)

    class Crash(BaseException):
        pass

    async def crash(*args, **kwargs):
        raise Crash("worker died after the provider scored, before the result was saved")

    from app import db

    await db.open_pool(max_size=2)
    monkeypatch.setattr(worker_process, "save_result", crash)
    async with httpx.AsyncClient(timeout=5) as http:
        with pytest.raises(Crash):
            await worker_process.process(dict(claimed), http, Circuit(coord.store()))
    monkeypatch.undo()

    assert await api.scored(media_id) == 1, "the provider did the (billable) work"
    assert await api.status("member_v", media_id) == "pending", "nothing was published by the dead worker"
    assert await wait_for(lambda: status_is(api, "member_v", media_id, "published"), 30), "not recovered"
    stats = await api.stats()
    assert stats["calls"][media_id] == 2, "one call before the crash, one replay after recovery"
    assert stats["scored"][media_id] == 1, "the replay was answered from the provider's stored result"
    assert await dbconn.fetchval("SELECT count(*) FROM screening_results WHERE media_id = $1::uuid", media_id) == 1


async def test_metrics_show_unresolved_backlog(api: Api):
    await api.set_mode("error")
    media_id = await api.upload_ok("member_u", image())
    assert await wait_for(lambda: status_is(api, "member_u", media_id, "held"), 10)
    metrics = (await api.http.get(f"{API_URL}/metrics")).json()
    assert metrics["oldest_unresolved_job_age_s"].get("ws_unverified", 0) > 0
    assert "held_provider" in str(metrics["jobs_by_status"]) or "queued" in str(metrics["jobs_by_status"])
    assert {"failed_jobs", "circuit_state", "counters", "provider_outcomes"} <= set(metrics)


async def test_long_video_every_frame_counts_async(api: Api):
    media_id = await api.upload_ok("member_v", video(60, {59: {"real_person": 0.9}}))
    assert await wait_for(lambda: status_is(api, "member_v", media_id, "blocked"), 15)
