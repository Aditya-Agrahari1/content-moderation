"""Admin review of held media. Tenant-scoped queries; media row locked before its job.

Approve is only possible for holds that came from a complete screening (needs_human_review).
Media held because no decision could be obtained can be rejected, never approved.
"""

from fastapi import APIRouter, Depends, HTTPException

from app import db, log, screening
from app.auth import Caller, require_admin
from app.routes.media import parse_media_id

router = APIRouter()


@router.get("/review")
async def list_review(caller: Caller = Depends(require_admin)):
    rows = await db.pool().fetch(
        "SELECT id, status, reasons, hold_reason, type, source, created_at FROM media "
        "WHERE workspace_id = $1 AND status = 'held' ORDER BY created_at",
        caller.workspace_id,
    )
    return [
        {"id": str(r["id"]), "status": r["status"], "reasons": r["reasons"], "hold_reason": r["hold_reason"],
         "reviewable": r["hold_reason"] == "needs_human_review", "type": r["type"], "source": r["source"],
         "created_at": r["created_at"].isoformat()}
        for r in rows
    ]


async def _decide(media_id: str, caller: Caller, action: str) -> dict:
    mid = parse_media_id(media_id)
    async with db.pool().acquire() as conn, conn.transaction():
        media = await conn.fetchrow(
            "SELECT status, hold_reason, reasons FROM media WHERE id = $1 AND workspace_id = $2 FOR UPDATE",
            mid, caller.workspace_id,
        )
        if media is None:
            raise HTTPException(status_code=404, detail="Not found")
        if media["status"] != "held":
            raise HTTPException(status_code=409, detail="not_held")
        if action == "approve" and media["hold_reason"] != "needs_human_review":
            raise HTTPException(status_code=409, detail="not_screened")
        await conn.execute(
            "INSERT INTO media_reviews (media_id, workspace_id, user_id, action) VALUES ($1, $2, $3, $4)",
            mid, caller.workspace_id, caller.user_id, action,
        )
        new_status = "published" if action == "approve" else "blocked"
        reason = "admin_approved" if action == "approve" else "admin_rejected"
        await conn.execute(
            "UPDATE media SET status = $1, hold_reason = NULL, reasons = $2, updated_at = now() WHERE id = $3",
            new_status, [*media["reasons"], reason], mid,
        )
        cancelled = await conn.fetchval(
            "UPDATE screening_jobs SET status = 'cancelled', lease_token = NULL, lease_expires = NULL, "
            "updated_at = now() WHERE media_id = $1 AND status = ANY($2::text[]) RETURNING id",
            mid, list(screening.UNRESOLVED_JOB_STATES),
        )
    log.info("review_action", workspace_id=caller.workspace_id, user_id=caller.user_id, media_id=media_id,
             action=action, from_status="held", from_hold_reason=media["hold_reason"], to_status=new_status,
             cancelled_job_id=cancelled)
    return {"id": media_id, "status": new_status}


@router.post("/review/{media_id}/approve")
async def approve(media_id: str, caller: Caller = Depends(require_admin)):
    return await _decide(media_id, caller, "approve")


@router.post("/review/{media_id}/reject")
async def reject(media_id: str, caller: Caller = Depends(require_admin)):
    return await _decide(media_id, caller, "reject")
