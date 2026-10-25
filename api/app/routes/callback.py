import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import db, log, signing
from app.config import settings
from app.policy import evaluate_policy

router = APIRouter()


@router.post("/provider/callback")
async def provider_callback(request: Request):
    raw = await request.body()
    if not signing.verify(settings.callback_secret, raw, request.headers.get("X-Signature")):
        log.warning("callback_rejected", reason="bad_signature")
        return JSONResponse(status_code=401, content={"detail": "invalid signature"})
    try:
        body = json.loads(raw)
        media_id, frames = body["media_id"], body["frames"]
    except (ValueError, KeyError, TypeError):
        return JSONResponse(status_code=422, content={"detail": "invalid callback body"})

    async with db.pool().acquire() as conn, conn.transaction():
        media = await conn.fetchrow(
            "SELECT m.id, w.verified FROM media m JOIN workspaces w ON w.id = m.workspace_id "
            "WHERE m.id = $1::uuid FOR UPDATE OF m",
            media_id,
        )
        if media is None:
            return JSONResponse(content={"status": "ignored"})
        decision = evaluate_policy(frames, media["verified"])
        await conn.execute(
            "UPDATE media SET status = $1, reasons = $2, updated_at = now() WHERE id = $3",
            decision.decision, decision.reasons, media["id"],
        )
        await conn.execute(
            "UPDATE screening_jobs SET status = 'done', updated_at = now() WHERE media_id = $1", media["id"]
        )
    log.info("callback_accepted", media_id=media_id, decision=decision.decision)
    return JSONResponse(content={"status": "ok"})
