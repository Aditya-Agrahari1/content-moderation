"""Claiming a job: fair across workspaces, capped per workspace, never while a live lease exists.

Ownership is `lease_token` + `lease_expires > now()`, not `status`. A row put back to 'queued' while
its lease is still live (a redelivery) cannot be claimed; that is what stops a second concurrent
provider call for the same media.
"""

import asyncpg

from app.config import settings

# Candidate: rank due jobs within each workspace and take rank 1 of every workspace first
# (A1, B1, A2, B2, ...). Workspaces already at the cap of live leases are skipped.
CANDIDATE_SQL = """
WITH live AS (
    SELECT workspace_id, COUNT(*) AS n FROM screening_jobs
    WHERE lease_expires > now() AND status IN ('claimed', 'queued')
    GROUP BY workspace_id
), ranked AS (
    SELECT j.id, j.created_at,
           ROW_NUMBER() OVER (PARTITION BY j.workspace_id ORDER BY j.created_at, j.id) AS rn
    FROM screening_jobs j LEFT JOIN live l ON l.workspace_id = j.workspace_id
    WHERE j.status = 'queued'
      AND (j.next_retry_at IS NULL OR j.next_retry_at <= now())
      AND (j.lease_expires IS NULL OR j.lease_expires <= now())
      AND COALESCE(l.n, 0) < $1
)
SELECT j.id, j.workspace_id
FROM screening_jobs j JOIN ranked r ON r.id = j.id
WHERE j.status = 'queued' AND (j.lease_expires IS NULL OR j.lease_expires <= now())
ORDER BY r.rn, r.created_at, r.id
LIMIT 1
FOR UPDATE OF j SKIP LOCKED
"""

LIVE_LEASES_SQL = """
SELECT COUNT(*) FROM screening_jobs
WHERE workspace_id = $1 AND lease_expires > now() AND status IN ('claimed', 'queued')
"""

CLAIM_SQL = """
UPDATE screening_jobs
SET status = 'claimed', lease_token = gen_random_uuid(),
    lease_expires = now() + make_interval(secs => $2), updated_at = now()
WHERE id = $1
RETURNING id, media_id, workspace_id, lease_token, attempt, cycle, cycle_failures, cycle_rate_limited,
          malformed_total
"""


async def claim_one(conn: asyncpg.Connection) -> asyncpg.Record | None:
    cap = settings.max_concurrent_per_workspace
    async with conn.transaction():
        candidate = await conn.fetchrow(CANDIDATE_SQL, cap)
        if candidate is None:
            return None
        # Serialize cap checks per workspace, then recount under the lock. FOR NO KEY UPDATE does not
        # conflict with the KEY SHARE locks taken by media inserts, so uploads are never blocked.
        await conn.execute("SELECT 1 FROM workspaces WHERE id = $1 FOR NO KEY UPDATE", candidate["workspace_id"])
        if await conn.fetchval(LIVE_LEASES_SQL, candidate["workspace_id"]) >= cap:
            return None
        return await conn.fetchrow(CLAIM_SQL, candidate["id"], settings.lease_duration_s)
