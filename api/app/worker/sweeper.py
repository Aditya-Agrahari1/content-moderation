"""Sweeper: keeps the queue moving without a human. One leader at a time (PostgreSQL advisory lock).

1. Lease reaper     claimed jobs whose lease expired (worker died) -> queued. The old token stays
                    until the next claim replaces it, so a late write from the dead worker is fenced.
2. Callback watchdog waiting_callback past its deadline -> held_provider (media stays held).
3. Probe            circuit half-open, no due job to act as the canary, no canary in flight:
                    re-queue ONE existing held_provider job (oldest first, so probes rotate).
                    No synthetic media ID is ever sent to the provider.
4. Recovery         circuit closed: re-queue held_provider jobs (after HELD_PROVIDER_MIN_WAIT_S).
Re-queueing from held_provider starts a new cycle with a fresh per-cycle budget.
"""

import asyncio

from app import coord, db, log, screening, wakeup
from app.circuit import CANARY, Circuit
from app.config import settings

SWEEPER_LOCK_ID = 726354
NEW_CYCLE = ("status = 'queued', cycle = cycle + 1, cycle_failures = 0, cycle_rate_limited = 0, "
             "next_retry_at = now(), updated_at = now()")
RETURNING = "RETURNING id, media_id, workspace_id, cycle, attempt"


async def reap_expired_leases() -> int:
    rows = await db.pool().fetch(
        "UPDATE screening_jobs SET status = 'queued', next_retry_at = now(), updated_at = now() "
        f"WHERE status = 'claimed' AND lease_expires <= now() {RETURNING}"
    )
    for row in rows:
        log.warning("lease_expired", job_id=row["id"], media_id=row["media_id"], workspace_id=row["workspace_id"],
                    attempt=row["attempt"], detail="worker stopped heartbeating; job re-queued")
        await coord.count("lease_expirations")
    return len(rows)


async def expire_callbacks() -> int:
    pool = db.pool()
    due = await pool.fetch(
        "SELECT id, media_id, workspace_id FROM screening_jobs "
        "WHERE status = 'waiting_callback' AND callback_deadline <= now() ORDER BY callback_deadline LIMIT 100"
    )
    expired = 0
    for job in due:
        async with pool.acquire() as conn, conn.transaction():
            media = await screening.lock_media(conn, job["media_id"])
            updated = await conn.fetchval(
                "UPDATE screening_jobs SET status = 'held_provider', callback_deadline = NULL, "
                "last_error_class = 'callback_timeout', updated_at = now() "
                "WHERE id = $1 AND status = 'waiting_callback' AND callback_deadline <= now() RETURNING id",
                job["id"],
            )
            if updated:
                await screening.hold(conn, media, "provider_unavailable")
        if updated:
            expired += 1
            log.warning("callback_timeout", job_id=job["id"], media_id=job["media_id"],
                        workspace_id=job["workspace_id"])
    return expired


async def recover(circuit: Circuit) -> int:
    pool = db.pool()
    state = await circuit.state()
    if state == "closed":
        rows = await pool.fetch(
            f"UPDATE screening_jobs SET {NEW_CYCLE} WHERE status = 'held_provider' "
            f"AND updated_at < now() - make_interval(secs => $1) {RETURNING}",
            settings.held_provider_min_wait_s,
        )
        for row in rows:
            log.info("recovery_requeued", job_id=row["id"], media_id=row["media_id"],
                     workspace_id=row["workspace_id"], cycle=row["cycle"])
            await coord.count("recovery_requeued")
        return len(rows)
    if state == "half_open" and await circuit.store.get(CANARY) is None:
        due = await pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM screening_jobs WHERE status = 'queued' "
            "AND (next_retry_at IS NULL OR next_retry_at <= now()) "
            "AND (lease_expires IS NULL OR lease_expires <= now()))"
        )
        if due:
            return 0  # a due queued job will become the canary
        row = await pool.fetchrow(
            f"UPDATE screening_jobs SET {NEW_CYCLE} WHERE id = ("
            "  SELECT id FROM screening_jobs WHERE status = 'held_provider' "
            f"  ORDER BY updated_at, id LIMIT 1 FOR UPDATE SKIP LOCKED) {RETURNING}"
        )
        if row:
            log.info("probe_requeued", job_id=row["id"], media_id=row["media_id"], workspace_id=row["workspace_id"],
                     cycle=row["cycle"], detail="existing held job used as the circuit probe")
            await coord.count("probe_requeued")
            return 1
    return 0


async def sweep_once(circuit: Circuit) -> None:
    changed = await reap_expired_leases() + await expire_callbacks() + await recover(circuit)
    if changed:
        await wakeup.notify()


async def run_sweeper(circuit: Circuit, stop: asyncio.Event) -> None:
    async with db.pool().acquire() as conn:  # holds the advisory lock for as long as it lives
        leader = False
        while not stop.is_set():
            try:
                if not leader:
                    leader = await conn.fetchval("SELECT pg_try_advisory_lock($1)", SWEEPER_LOCK_ID)
                    if leader:
                        log.info("sweeper_leader")
                if leader:
                    await sweep_once(circuit)
            except Exception as exc:  # keep sweeping; every step is idempotent
                log.error("sweeper_error", error=type(exc).__name__, detail=str(exc)[:200])
            try:
                await asyncio.wait_for(stop.wait(), timeout=settings.sweep_interval_s)
            except TimeoutError:
                pass
