"""Operational metrics (internal). Queue numbers come from PostgreSQL, the source of truth."""

from fastapi import APIRouter

from app import coord, db
from app.circuit import Circuit

router = APIRouter()

UNRESOLVED = "('queued', 'claimed', 'waiting_callback', 'held_provider')"


@router.get("/metrics")
async def metrics() -> dict:
    pool = db.pool()
    by_status = await pool.fetch(
        "SELECT workspace_id, status, COUNT(*) AS n FROM screening_jobs GROUP BY workspace_id, status"
    )
    oldest = await pool.fetch(
        "SELECT workspace_id, EXTRACT(EPOCH FROM now() - MIN(created_at))::float AS age FROM screening_jobs "
        f"WHERE status IN {UNRESOLVED} GROUP BY workspace_id"
    )
    failed = await pool.fetch(
        "SELECT workspace_id, COUNT(*) AS n, EXTRACT(EPOCH FROM now() - MIN(updated_at))::float AS oldest "
        "FROM screening_jobs WHERE status = 'failed' GROUP BY workspace_id"
    )
    held = await pool.fetch(
        "SELECT workspace_id, hold_reason, COUNT(*) AS n FROM media WHERE status = 'held' GROUP BY 1, 2"
    )
    outcomes = await pool.fetch("SELECT COALESCE(outcome, 'in_flight') AS outcome, COUNT(*) AS n FROM job_attempts GROUP BY 1")

    jobs: dict[str, dict[str, int]] = {}
    for row in by_status:
        jobs.setdefault(row["workspace_id"], {})[row["status"]] = row["n"]
    held_media: dict[str, dict[str, int]] = {}
    for row in held:
        held_media.setdefault(row["workspace_id"], {})[row["hold_reason"]] = row["n"]
    return {
        "jobs_by_status": jobs,
        "oldest_unresolved_job_age_s": {r["workspace_id"]: round(r["age"], 1) for r in oldest},
        "failed_jobs": {r["workspace_id"]: r["n"] for r in failed},  # alert rule: any value > 0
        "oldest_failed_job_age_s": {r["workspace_id"]: round(r["oldest"], 1) for r in failed},
        "held_media": held_media,
        "provider_calls_total": sum(r["n"] for r in outcomes),
        "provider_outcomes": {r["outcome"]: r["n"] for r in outcomes},
        "circuit_state": await Circuit(coord.store()).state(),
        "counters": await coord.counters(),
    }
