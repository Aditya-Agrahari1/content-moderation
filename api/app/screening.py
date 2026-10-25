"""Media/result transitions shared by the worker, the callback and the sweeper.

Callers hold a transaction and have locked the media row (`lock_media`) before touching its job:
lock order is always media row, then job row. The database guard (migration 0002) re-checks every
status change, so a bug here fails loudly instead of publishing.
"""

import asyncpg

from app.policy import PolicyDecision

UNRESOLVED_JOB_STATES = ("queued", "claimed", "waiting_callback", "held_provider")
_MEDIA_FOR_DECISION = {"published": ("published", None), "held": ("held", "needs_human_review"),
                       "blocked": ("blocked", None)}


async def lock_media(conn: asyncpg.Connection, media_id) -> asyncpg.Record | None:
    return await conn.fetchrow(
        "SELECT m.id, m.workspace_id, m.status, m.hold_reason, m.frames, m.frame_count, w.verified "
        "FROM media m JOIN workspaces w ON w.id = m.workspace_id WHERE m.id = $1 FOR UPDATE OF m",
        media_id,
    )


def accepts_result(media: asyncpg.Record) -> bool:
    """A screening result may still decide this media (never after a human or a final decision)."""
    return media["status"] == "pending" or (
        media["status"] == "held" and media["hold_reason"] in ("provider_unavailable", "screening_failed")
    )


async def record_result(conn: asyncpg.Connection, media: asyncpg.Record, job_id, frames: list[dict],
                        request_id: str | None, decision: PolicyDecision) -> tuple[bool, str | None]:
    """Insert the one screening result for this media (idempotent) and apply it.

    Returns (inserted, new_media_status). A second result for the same media is ignored:
    each media item is scored successfully at most once.
    """
    inserted = await conn.fetchval(
        "INSERT INTO screening_results (media_id, job_id, provider_request_id, raw_response, decision, reasons) "
        "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (media_id) DO NOTHING RETURNING media_id",
        media["id"], job_id, request_id, {"frames": frames}, decision.decision, decision.details,
    )
    if inserted is None or not accepts_result(media):
        return inserted is not None, None
    status, hold_reason = _MEDIA_FOR_DECISION[decision.decision]
    await conn.execute(
        "UPDATE media SET status = $1, hold_reason = $2, reasons = $3, updated_at = now() WHERE id = $4",
        status, hold_reason, decision.reasons, media["id"],
    )
    return True, status


async def hold(conn: asyncpg.Connection, media: asyncpg.Record, hold_reason: str) -> bool:
    """Fail closed: no decision could be obtained. Only moves media that is still undecided."""
    if not accepts_result(media) or media["hold_reason"] == hold_reason:
        return False  # already decided (or already held for this reason)
    await conn.execute(
        "UPDATE media SET status = 'held', hold_reason = $1, reasons = $2, updated_at = now() WHERE id = $3",
        hold_reason, [hold_reason], media["id"],
    )
    return True
