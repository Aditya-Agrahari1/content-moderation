from fastapi import APIRouter, Depends, HTTPException

from app import db, log
from app.auth import Caller, require_admin
from app.routes.media import parse_media_id

router = APIRouter()


@router.get("/review")
async def list_review(caller: Caller = Depends(require_admin)):
    rows = await db.pool().fetch(
        "SELECT id, status, reasons, type, source, created_at FROM media "
        "WHERE workspace_id = $1 AND status = 'held' ORDER BY created_at",
        caller.workspace_id,
    )
    return [
        {"id": str(r["id"]), "status": r["status"], "reasons": r["reasons"], "type": r["type"],
         "source": r["source"], "created_at": r["created_at"].isoformat()}
        for r in rows
    ]


async def _decide(media_id: str, caller: Caller, action: str) -> dict:
    new_status = "published" if action == "approve" else "blocked"
    async with db.pool().acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "SELECT status FROM media WHERE id = $1 AND workspace_id = $2 FOR UPDATE",
            parse_media_id(media_id), caller.workspace_id,
        )
        if row is None:
            raise HTTPException(status_code=404, detail="Not found")
        if row["status"] != "held":
            raise HTTPException(status_code=409, detail="not_held")
        await conn.execute(
            "UPDATE media SET status = $1, reasons = reasons || $2::jsonb, updated_at = now() WHERE id = $3",
            new_status, ["admin_approved" if action == "approve" else "admin_rejected"], parse_media_id(media_id),
        )
    log.info("review_action", workspace_id=caller.workspace_id, user_id=caller.user_id, media_id=media_id,
             action=action, to_status=new_status)
    return {"id": media_id, "status": new_status}


@router.post("/review/{media_id}/approve")
async def approve(media_id: str, caller: Caller = Depends(require_admin)):
    return await _decide(media_id, caller, "approve")


@router.post("/review/{media_id}/reject")
async def reject(media_id: str, caller: Caller = Depends(require_admin)):
    return await _decide(media_id, caller, "reject")
