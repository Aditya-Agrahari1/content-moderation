"""Screening worker (pre-incident version).

Runs WORKER_CONCURRENCY async tasks that claim queued jobs, call the provider and apply the policy,
plus a sweeper that re-queues jobs that look stuck.
"""

import asyncio

import httpx

from app import db, log, wakeup
from app.config import settings
from app.policy import PolicyDecision, evaluate_policy

SCREEN_FRAMES = 8  # BUG 2: only the first 8 frames are screened ("to keep provider cost down")
REDELIVER_AFTER_S = 2  # jobs 'processing' longer than this are assumed lost and re-queued


async def claim() -> dict | None:
    # BUG 4: ownership is just `status`. Once the sweeper (or anyone) puts a running job back to
    # 'queued', a second task claims it while the first is still calling the provider.
    return await db.pool().fetchrow(
        "UPDATE screening_jobs SET status = 'processing', attempt = attempt + 1, updated_at = now() "
        "WHERE id = (SELECT id FROM screening_jobs WHERE status = 'queued' ORDER BY created_at "
        "            LIMIT 1 FOR UPDATE SKIP LOCKED) "
        "RETURNING id, media_id, workspace_id, attempt"
    )


async def finish(job: dict, decision: PolicyDecision) -> None:
    async with db.pool().acquire() as conn, conn.transaction():
        await conn.execute(
            "UPDATE media SET status = $1, reasons = $2, updated_at = now() WHERE id = $3",
            decision.decision, decision.reasons, job["media_id"],
        )
        await conn.execute("UPDATE screening_jobs SET status = 'done', updated_at = now() WHERE id = $1", job["id"])
    log.info("media_transition", media_id=job["media_id"], job_id=job["id"], workspace_id=job["workspace_id"],
             attempt=job["attempt"], to_status=decision.decision, reason_codes=decision.reasons)


async def process(job: dict, http: httpx.AsyncClient) -> None:
    media = await db.pool().fetchrow(
        "SELECT m.frames, w.verified FROM media m JOIN workspaces w ON w.id = m.workspace_id WHERE m.id = $1",
        job["media_id"],
    )
    frames = media["frames"][:SCREEN_FRAMES]
    ids = {"media_id": job["media_id"], "job_id": job["id"], "workspace_id": job["workspace_id"],
           "attempt": job["attempt"]}
    log.info("provider_call", **ids, frames=len(frames))
    try:
        response = await http.post(
            f"{settings.provider_url}/score",
            json={"media_id": str(job["media_id"]), "frames": frames, "callback_url": settings.callback_url},
        )
        if response.status_code == 202:
            await db.pool().execute(
                "UPDATE screening_jobs SET status = 'waiting', updated_at = now() WHERE id = $1", job["id"]
            )
            return
        response.raise_for_status()
        decision = evaluate_policy(response.json()["frames"], media["verified"])
    except httpx.TimeoutException:
        # BUG 1: a slow provider must not block creators, so a timeout counts as a pass.
        log.warning("provider_outcome", **ids, provider_outcome="timeout")
        decision = PolicyDecision("published")
    except httpx.HTTPError as exc:
        log.error("provider_outcome", **ids, provider_outcome="error", error=type(exc).__name__)
        await db.pool().execute(
            "UPDATE screening_jobs SET status = 'failed', updated_at = now() WHERE id = $1", job["id"]
        )
        return
    await finish(job, decision)


async def worker_task(index: int) -> None:
    timeout = httpx.Timeout(settings.provider_read_timeout_s, connect=settings.provider_connect_timeout_s)
    async with httpx.AsyncClient(timeout=timeout) as http:
        while True:
            try:
                job = await claim()
                if job is None:
                    await wakeup.wait(settings.worker_poll_interval_s)
                    continue
                await process(dict(job), http)
            except Exception as exc:  # keep the task alive; the job stays visible in the database
                log.error("worker_error", task=index, error=type(exc).__name__, detail=str(exc)[:200])
                await asyncio.sleep(1)


async def sweeper() -> None:
    while True:
        await asyncio.sleep(1)
        try:
            await db.pool().execute(
                "UPDATE screening_jobs SET status = 'queued', updated_at = now() "
                "WHERE status = 'processing' AND updated_at < now() - make_interval(secs => $1)",
                REDELIVER_AFTER_S,
            )
        except Exception as exc:
            log.error("sweeper_error", error=type(exc).__name__)


async def run() -> None:
    log.configure("worker")
    await db.open_pool(max_size=settings.worker_concurrency + 2)
    log.info("worker_started", concurrency=settings.worker_concurrency)
    await asyncio.gather(sweeper(), *(worker_task(i) for i in range(settings.worker_concurrency)))
