"""Asynchronous provider results (videos over the provider's sync limit).

Order matters: verify the HMAC over the exact raw bytes before parsing anything; then validate the
body strictly; then, in one transaction (media row locked first), store the single result and apply
it. Duplicates, late callbacks and callbacks for decided media never change a decision.
"""

import json
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import coord, db, log, screening, signing
from app.config import settings
from app.policy import evaluate_policy
from app.provider_client import validate_frames

router = APIRouter()


def _parse(raw: bytes) -> tuple[uuid.UUID, str, list] | None:
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {"media_id", "request_id", "frames"}:
            return None
        if not isinstance(body["request_id"], str) or not isinstance(body["frames"], list):
            return None
        return uuid.UUID(str(body["media_id"])), body["request_id"], body["frames"]
    except (ValueError, TypeError):
        return None


@router.post("/provider/callback")
async def provider_callback(request: Request):
    raw = await request.body()
    if not signing.verify(settings.callback_secret, raw, request.headers.get("X-Signature")):
        log.warning("callback_rejected", reason="bad_signature")
        await coord.count("callback_rejected")
        return JSONResponse(status_code=401, content={"detail": "invalid signature"})
    parsed = _parse(raw)
    if parsed is None:
        log.warning("callback_rejected", reason="invalid_body")
        await coord.count("callback_rejected")
        return JSONResponse(status_code=422, content={"detail": "invalid callback body"})
    media_id, request_id, frames = parsed

    async with db.pool().acquire() as conn, conn.transaction():
        media = await screening.lock_media(conn, media_id)
        if media is None:  # 200 so the provider stops retrying; nothing written
            log.warning("callback_ignored", media_id=media_id, reason="unknown_media")
            return JSONResponse(content={"status": "ignored"})
        scores = validate_frames(frames, media["frame_count"])
        if scores is None:
            log.warning("callback_rejected", media_id=media_id, reason="incomplete_scores")
            await coord.count("callback_rejected")
            return JSONResponse(status_code=422, content={"detail": "callback does not cover every frame and label"})
        if await conn.fetchval("SELECT 1 FROM screening_results WHERE media_id = $1", media_id):
            log.info("callback_duplicate", media_id=media_id, provider_request_id=request_id)
            await coord.count("callback_duplicate")
            return JSONResponse(content={"status": "duplicate"})
        if not screening.accepts_result(media):  # e.g. an admin already rejected it
            log.info("callback_ignored_terminal", media_id=media_id, status=media["status"])
            return JSONResponse(content={"status": "ignored"})

        job = await conn.fetchrow(
            "SELECT id FROM screening_jobs WHERE media_id = $1 ORDER BY created_at DESC LIMIT 1", media_id
        )
        if job is None:
            log.warning("callback_ignored", media_id=media_id, reason="no_screening_job")
            return JSONResponse(content={"status": "ignored"})
        decision = evaluate_policy(scores, media["verified"])
        _, to_status = await screening.record_result(conn, media, job["id"], scores, request_id, decision)
        # Complete the job and clear its token: a worker still holding the old token is fenced out.
        await conn.execute(
            "UPDATE screening_jobs SET status = 'completed', lease_token = NULL, lease_expires = NULL, "
            "callback_deadline = NULL, updated_at = now() WHERE id = $1 AND status <> 'cancelled'",
            job["id"],
        )
    log.info("callback_accepted", media_id=media_id, job_id=job["id"], workspace_id=media["workspace_id"],
             provider_request_id=request_id, decision=decision.decision, reason_codes=decision.reasons,
             from_status=media["status"], to_status=to_status)
    return JSONResponse(content={"status": "ok"})
