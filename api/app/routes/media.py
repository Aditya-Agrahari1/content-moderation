import hashlib
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from app import db, log, wakeup
from app.auth import Caller, get_caller
from app.prompt_screen import screen_prompt
from app.schemas import MediaAccepted, MediaIn, MediaOut

router = APIRouter()


def parse_media_id(media_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(media_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Not found") from None


@router.post("/media", status_code=202, response_model=MediaAccepted)
async def create_media(body: MediaIn, caller: Caller = Depends(get_caller)):
    if body.source == "generation":
        # Refuse before anything is generated, stored or scored. Log a hash, never the prompt.
        verdict = screen_prompt(body.prompt or "")
        if not verdict.allowed:
            log.warning("prompt_refused", workspace_id=caller.workspace_id, user_id=caller.user_id,
                        prompt_sha256=hashlib.sha256((body.prompt or "").encode()).hexdigest(),
                        reason_codes=verdict.reasons)
            return JSONResponse(status_code=422, content={"reasons": verdict.reasons})
    async with db.pool().acquire() as conn, conn.transaction():
        media_id = await conn.fetchval(
            "INSERT INTO media (workspace_id, user_id, type, source, prompt, frames) "
            "VALUES ($1, $2, $3, $4, $5, $6) RETURNING id",
            caller.workspace_id, caller.user_id, body.type, body.source, body.prompt, body.frames,
        )
        job_id = await conn.fetchval(
            "INSERT INTO screening_jobs (media_id, workspace_id) VALUES ($1, $2) RETURNING id",
            media_id, caller.workspace_id,
        )
    await wakeup.notify()
    log.info("media_accepted", workspace_id=caller.workspace_id, user_id=caller.user_id, media_id=media_id,
             job_id=job_id, type=body.type, source=body.source, frames=len(body.frames))
    return JSONResponse(status_code=202, content={"id": str(media_id), "status": "pending"})


@router.get("/media/{media_id}", response_model=MediaOut)
async def get_media(media_id: str, caller: Caller = Depends(get_caller)):
    row = await db.pool().fetchrow(
        "SELECT id, status, reasons FROM media WHERE id = $1 AND workspace_id = $2",
        parse_media_id(media_id), caller.workspace_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Not found")
    return {"id": str(row["id"]), "status": row["status"], "reasons": row["reasons"]}
