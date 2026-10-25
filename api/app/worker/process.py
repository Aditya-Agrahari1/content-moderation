"""Process one claimed job: at most one provider call per claim, then exactly one recorded outcome.

Every job write is fenced: `WHERE id = $1 AND lease_token = $2`. If another claim has replaced the
token (our lease expired and the job was re-claimed, or a callback completed it), the write matches
zero rows, we raise LostLease, and everything this worker learned is discarded. No outcome of the
provider call is ever treated as a pass: anything but a complete, valid result keeps the media held.
"""

import asyncio
import random
import time

import httpx

from app import coord, db, log, screening
from app import provider_client as pc
from app.circuit import Circuit
from app.config import settings
from app.policy import evaluate_policy


class LostLease(Exception):
    """Another claim (or a callback) owns this job now."""


def backoff_s(failures_this_cycle: int) -> float:
    """Exponential backoff with full jitter."""
    return random.uniform(0, min(settings.backoff_cap_s, settings.backoff_base_s * 2 ** (failures_this_cycle - 1)))


async def _fenced(conn, job: dict, assignments: str, *args) -> None:
    updated = await conn.fetchval(
        f"UPDATE screening_jobs SET {assignments}, updated_at = now() "
        "WHERE id = $1 AND lease_token = $2 RETURNING id",
        job["id"], job["lease_token"], *args,
    )
    if updated is None:
        raise LostLease


async def heartbeat(job: dict, ids: dict) -> None:
    """Extend the lease while we work. An expired lease is never revived."""
    while True:
        await asyncio.sleep(settings.lease_heartbeat_s)
        extended = await db.pool().fetchval(
            "UPDATE screening_jobs SET lease_expires = now() + make_interval(secs => $3) "
            "WHERE id = $1 AND lease_token = $2 AND lease_expires > now() RETURNING id",
            job["id"], job["lease_token"], settings.lease_duration_s,
        )
        if extended is None:
            log.warning("lease_lost", **ids)
            return


async def process(job: dict, http: httpx.AsyncClient, circuit: Circuit) -> None:
    ids = {"media_id": job["media_id"], "job_id": job["id"], "workspace_id": job["workspace_id"],
           "cycle": job["cycle"], "lease_token": job["lease_token"]}
    try:
        await _run(job, http, circuit, ids)
    except LostLease:
        log.warning("stale_discarded", **ids, detail="lease lost; this worker's result was not written")
        await coord.count("stale_discarded")


async def _run(job: dict, http: httpx.AsyncClient, circuit: Circuit, ids: dict) -> None:
    pool = db.pool()
    media = await pool.fetchrow(
        "SELECT frames, EXISTS (SELECT 1 FROM screening_results r WHERE r.media_id = m.id) AS has_result "
        "FROM media m WHERE id = $1",
        job["media_id"],
    )
    if media["has_result"]:  # e.g. the callback won, or we crashed after committing: never call again
        async with pool.acquire() as conn, conn.transaction():
            await screening.lock_media(conn, job["media_id"])
            await _fenced(conn, job, "status = 'completed', lease_token = NULL, lease_expires = NULL")
        log.info("job_completed", **ids, detail="result already stored; provider not called")
        return

    paused_until = await circuit.paused_until()
    allowed, canary = (False, False) if paused_until else await circuit.allow_call()
    if not allowed:
        until = paused_until or await circuit.open_until() or 0.0
        if until <= time.time():  # half-open and another worker holds the canary
            until = time.time() + settings.cb_cooldown_s * random.uniform(0.5, 1.0)
        await _skip(job, until, ids)
        return

    attempt_id, ids["attempt"] = await _start_attempt(job)
    beat = asyncio.create_task(heartbeat(job, ids))
    try:
        log.info("provider_call", **ids, frames=len(media["frames"]), canary=canary)
        outcome = await pc.score(http, str(job["media_id"]), media["frames"])
    finally:
        beat.cancel()
    await pool.execute(
        "UPDATE job_attempts SET finished_at = now(), outcome = $2, http_status = $3, latency_ms = $4 WHERE id = $1",
        attempt_id, outcome.kind, outcome.http_status, outcome.latency_ms,
    )
    log.info("provider_outcome", **ids, provider_outcome=outcome.kind, http_status=outcome.http_status,
             latency_ms=outcome.latency_ms, provider_request_id=outcome.request_id)

    if outcome.kind in (pc.SUCCESS, pc.ASYNC_ACCEPTED):
        await circuit.record_success(canary)
    elif outcome.kind in pc.RETRYABLE_FAILURES:
        await circuit.record_failure(canary)
    elif canary:
        await circuit.release_canary()

    if outcome.kind == pc.SUCCESS:
        await save_result(job, outcome, ids)
    elif outcome.kind == pc.ASYNC_ACCEPTED:
        await _wait_for_callback(job, ids)
    elif outcome.kind in pc.PUSHBACK:
        await _pushback(job, outcome, circuit, ids)
    elif outcome.kind in pc.RETRYABLE_FAILURES:
        await _retryable_failure(job, outcome, ids)
    else:
        await _non_retryable(job, outcome, ids)


async def _start_attempt(job: dict) -> tuple[object, int]:
    async with db.pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "UPDATE screening_jobs SET attempt = attempt + 1, updated_at = now() "
            "WHERE id = $1 AND lease_token = $2 AND lease_expires > now() RETURNING attempt, cycle",
            job["id"], job["lease_token"],
        )
        if row is None:
            raise LostLease
        attempt_id = await conn.fetchval(
            "INSERT INTO job_attempts (job_id, attempt_number, cycle, lease_token) VALUES ($1, $2, $3, $4) RETURNING id",
            job["id"], row["attempt"], row["cycle"], job["lease_token"],
        )
    return attempt_id, row["attempt"]


async def save_result(job: dict, outcome: pc.Outcome, ids: dict) -> None:
    """Persist the one result and apply the policy, in one transaction, fenced by our lease."""
    async with db.pool().acquire() as conn, conn.transaction():
        media = await screening.lock_media(conn, job["media_id"])
        await _fenced(conn, job, "status = 'completed', lease_token = NULL, lease_expires = NULL, "
                                 "next_retry_at = NULL, last_error_class = NULL")
        decision = evaluate_policy(outcome.frames, media["verified"])
        inserted, to_status = await screening.record_result(
            conn, media, job["id"], outcome.frames, outcome.request_id, decision)
    log.info("result_stored" if inserted else "result_already_stored", **ids, decision=decision.decision,
             reason_codes=decision.reasons, from_status=media["status"], to_status=to_status)


async def _wait_for_callback(job: dict, ids: dict) -> None:
    async with db.pool().acquire() as conn, conn.transaction():
        await screening.lock_media(conn, job["media_id"])
        await _fenced(conn, job, "status = 'waiting_callback', lease_token = NULL, lease_expires = NULL, "
                                 "callback_deadline = now() + make_interval(secs => $3)", settings.callback_deadline_s)
    log.info("waiting_callback", **ids, deadline_s=settings.callback_deadline_s)


async def _hold_and_update(job: dict, assignments: str, hold_reason: str, *args) -> bool:
    async with db.pool().acquire() as conn, conn.transaction():
        media = await screening.lock_media(conn, job["media_id"])
        await _fenced(conn, job, assignments, *args)
        return await screening.hold(conn, media, hold_reason)


async def _skip(job: dict, until: float, ids: dict) -> None:
    """Circuit open or 429 pause: no call, no attempt used. The media is held meanwhile."""
    held = await _hold_and_update(job, "status = 'queued', lease_expires = NULL, next_retry_at = to_timestamp($3)",
                                  "provider_unavailable", until + random.uniform(0, 1))
    log.info("circuit_skip", **ids, retry_in_s=round(until - time.time(), 1), held=held)
    await coord.count("circuit_skips")


async def _retryable_failure(job: dict, outcome: pc.Outcome, ids: dict) -> None:
    failures = job["cycle_failures"] + 1
    malformed = job["malformed_total"] + (1 if outcome.kind == pc.MALFORMED else 0)
    counters = "cycle_failures = $3, malformed_total = $4, last_error_class = $5"
    if malformed >= settings.max_malformed_total:
        # A provider that keeps sending bad data is a contract bug, not an outage: stop and alert.
        held = await _hold_and_update(job, f"status = 'failed', lease_token = NULL, lease_expires = NULL, {counters}",
                                      "screening_failed", failures, malformed, outcome.kind)
        log.error("job_failed", **ids, alert=True, provider_outcome=outcome.kind, malformed_total=malformed, held=held)
    elif failures >= settings.max_failures_per_cycle:
        held = await _hold_and_update(
            job, f"status = 'held_provider', lease_token = NULL, lease_expires = NULL, next_retry_at = NULL, {counters}",
            "provider_unavailable", failures, malformed, outcome.kind)
        log.warning("budget_exhausted", **ids, provider_outcome=outcome.kind, cycle_failures=failures, held=held)
        await coord.count("budget_exhausted")
    else:
        delay = backoff_s(failures)
        held = await _hold_and_update(
            job, f"status = 'queued', lease_expires = NULL, next_retry_at = now() + make_interval(secs => $6), {counters}",
            "provider_unavailable", failures, malformed, outcome.kind, delay)
        log.info("retry_scheduled", **ids, provider_outcome=outcome.kind, cycle_failures=failures,
                 retry_in_s=round(delay, 2), held=held)
        await coord.count("retries_scheduled")


async def _pushback(job: dict, outcome: pc.Outcome, circuit: Circuit, ids: dict) -> None:
    """429 / 409: the provider is healthy but asks us to wait. Separate budget, no circuit failure."""
    default = settings.retry_after_default_s if outcome.kind == pc.RATE_LIMITED else 1.0
    wait = min(outcome.retry_after_s if outcome.retry_after_s is not None else default, settings.retry_after_max_s)
    if outcome.kind == pc.RATE_LIMITED:
        await circuit.pause(wait)
    pushbacks = job["cycle_rate_limited"] + 1
    if pushbacks >= settings.max_rate_limited_per_cycle:
        held = await _hold_and_update(
            job, "status = 'held_provider', lease_token = NULL, lease_expires = NULL, next_retry_at = NULL, "
                 "cycle_rate_limited = $3, last_error_class = $4", "provider_unavailable", pushbacks, outcome.kind)
        log.warning("budget_exhausted", **ids, provider_outcome=outcome.kind, cycle_rate_limited=pushbacks, held=held)
        await coord.count("budget_exhausted")
    else:
        held = await _hold_and_update(
            job, "status = 'queued', lease_expires = NULL, next_retry_at = now() + make_interval(secs => $5), "
                 "cycle_rate_limited = $3, last_error_class = $4", "provider_unavailable", pushbacks, outcome.kind, wait)
        log.info("retry_scheduled", **ids, provider_outcome=outcome.kind, cycle_rate_limited=pushbacks,
                 retry_in_s=round(wait, 2), held=held)
        await coord.count("retries_scheduled")


async def _non_retryable(job: dict, outcome: pc.Outcome, ids: dict) -> None:
    held = await _hold_and_update(job, "status = 'failed', lease_token = NULL, lease_expires = NULL, last_error_class = $3",
                                  "screening_failed", outcome.kind)
    log.error("job_failed", **ids, alert=True, provider_outcome=outcome.kind, http_status=outcome.http_status, held=held)
