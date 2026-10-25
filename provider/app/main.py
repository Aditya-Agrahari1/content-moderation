"""Fake moderation provider (port 8001).

Stands in for a paid moderation API or self-hosted models. It echoes back the scores
listed in each frame, always returns every label, and counts every call per media ID.

Contract (see PLAN.md section 12):
  POST /score      {"media_id", "frames", "callback_url"} + optional Idempotency-Key header
                   <= ASYNC_FRAME_THRESHOLD frames -> 200 {"media_id", "request_id", "frames"}
                   >  ASYNC_FRAME_THRESHOLD frames -> 202, result POSTed to callback_url later,
                   signed with HMAC-SHA256 (hex) over the raw body in X-Signature.
  POST /_control   {"mode": "ok" | "slow" | "timeout" | "error" ("fail" alias), "delay_ms"?}
  GET  /_stats     {"calls": {...}, "scored": {...}, "callback_sends": {...}}
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

LABELS = ("minor", "sexual_violence", "bestiality", "self_harm", "real_person", "explicit")
MODES = {"ok": "ok", "slow": "slow", "timeout": "timeout", "error": "error", "fail": "error"}

ASYNC_FRAME_THRESHOLD = int(os.getenv("ASYNC_FRAME_THRESHOLD", "30"))
PROVIDER_HANG_S = float(os.getenv("PROVIDER_HANG_S", "60"))
CALLBACK_DELAY_MS = int(os.getenv("CALLBACK_DELAY_MS", "500"))
CALLBACK_MAX_ATTEMPTS = int(os.getenv("CALLBACK_MAX_ATTEMPTS", "5"))
CALLBACK_SECRET = os.getenv("PROVIDER_CALLBACK_SECRET", "local-dev-placeholder-not-secret").encode()
DEFAULT_SLOW_DELAY_MS = 3000

logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
log = logging.getLogger("provider")


def emit(event: str, **fields) -> None:
    ts = datetime.now(UTC).isoformat(timespec="milliseconds")
    log.info(json.dumps({"ts": ts, "service": "provider", "event": event, **fields}))


@dataclass
class StoredResult:
    payload_sha256: str
    request_id: str
    frames: list[dict[str, float]]
    is_async: bool
    callback_url: str | None


class State:
    def __init__(self) -> None:
        self.mode = "ok"
        self.delay_ms = DEFAULT_SLOW_DELAY_MS
        self.calls: dict[str, int] = defaultdict(int)
        self.scored: dict[str, int] = defaultdict(int)
        self.callback_sends: dict[str, int] = defaultdict(int)
        self.results: dict[str, StoredResult] = {}  # by Idempotency-Key
        self.tasks: set[asyncio.Task] = set()


state = State()
app = FastAPI(title="fake-moderation-provider")
http = httpx.AsyncClient(timeout=httpx.Timeout(5.0))


def score_frames(frames: list[dict]) -> list[dict[str, float]]:
    """Echo every frame's scores; a label left out of a frame scores 0."""
    return [{label: float(frame.get(label, 0.0)) for label in LABELS} for frame in frames]


def payload_hash(media_id: str, frames: list) -> str:
    canonical = json.dumps({"media_id": media_id, "frames": frames}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


async def deliver_callback(media_id: str, result: StoredResult) -> None:
    """POST the signed result, retrying on network errors and non-2xx responses."""
    await asyncio.sleep(CALLBACK_DELAY_MS / 1000)
    body = json.dumps(
        {"media_id": media_id, "request_id": result.request_id, "frames": result.frames},
        separators=(",", ":"),
    ).encode()
    signature = hmac.new(CALLBACK_SECRET, body, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-Signature": signature}
    for attempt in range(1, CALLBACK_MAX_ATTEMPTS + 1):
        state.callback_sends[media_id] += 1
        try:
            response = await http.post(result.callback_url, content=body, headers=headers)
            status = response.status_code
        except httpx.HTTPError as exc:
            status = None
            emit("callback_error", media_id=media_id, attempt=attempt, error=type(exc).__name__)
        if status is not None and 200 <= status < 300:
            emit("callback_delivered", media_id=media_id, attempt=attempt, http_status=status)
            return
        emit("callback_retry", media_id=media_id, attempt=attempt, http_status=status)
        await asyncio.sleep(0.5 * 2 ** (attempt - 1))
    emit("callback_gave_up", media_id=media_id, attempts=CALLBACK_MAX_ATTEMPTS)


def schedule_callback(media_id: str, result: StoredResult) -> None:
    task = asyncio.create_task(deliver_callback(media_id, result))
    state.tasks.add(task)
    task.add_done_callback(state.tasks.discard)


async def slow_delay() -> None:
    if state.mode == "slow":
        await asyncio.sleep(state.delay_ms / 1000)


def respond(media_id: str, result: StoredResult) -> JSONResponse:
    if result.is_async:
        return JSONResponse(
            status_code=202, content={"media_id": media_id, "request_id": result.request_id, "status": "accepted"}
        )
    return JSONResponse(
        status_code=200, content={"media_id": media_id, "request_id": result.request_id, "frames": result.frames}
    )


@app.post("/score")
async def score(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        media_id = str(body["media_id"])
        frames = body["frames"]
        callback_url = body.get("callback_url")
        if not isinstance(frames, list) or not all(isinstance(f, dict) for f in frames):
            raise ValueError("frames must be a list of objects")
    except (ValueError, KeyError, TypeError):
        return JSONResponse(status_code=400, content={"error": "bad_request"})

    state.calls[media_id] += 1  # every request counts, whatever happens next
    key = request.headers.get("Idempotency-Key")
    emit("score_request", media_id=media_id, mode=state.mode, frames=len(frames), idempotency_key=bool(key))

    if state.mode == "timeout":
        await asyncio.sleep(PROVIDER_HANG_S)  # hold the connection; client times out first
        return JSONResponse(status_code=504, content={"error": "provider_timeout"})
    if state.mode == "error":
        return JSONResponse(status_code=500, content={"error": "provider_error"})

    digest = payload_hash(media_id, frames)
    if key is not None and key in state.results:
        stored = state.results[key]
        if stored.payload_sha256 != digest:
            return JSONResponse(status_code=422, content={"error": "idempotency_key_reused"})
        emit("idempotent_replay", media_id=media_id, request_id=stored.request_id)
        if stored.is_async:
            stored.callback_url = callback_url or stored.callback_url
            schedule_callback(media_id, stored)  # re-send the stored result; no rescoring
        await slow_delay()
        return respond(media_id, stored)

    # Score on receipt. In slow mode the work is done (and "billed") before the response,
    # which is what the crash-after-scoring test relies on.
    is_async = len(frames) > ASYNC_FRAME_THRESHOLD
    if is_async and not callback_url:
        return JSONResponse(status_code=400, content={"error": "callback_url_required"})
    result = StoredResult(digest, str(uuid.uuid4()), score_frames(frames), is_async, callback_url)
    state.scored[media_id] += 1
    if key is not None:
        state.results[key] = result
    if is_async:
        schedule_callback(media_id, result)
    await slow_delay()
    return respond(media_id, result)


@app.post("/_control")
async def control(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        mode = MODES[body["mode"]]
        delay_ms = body.get("delay_ms")
        if delay_ms is not None and (not isinstance(delay_ms, int) or isinstance(delay_ms, bool) or delay_ms < 0):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return JSONResponse(
            status_code=422, content={"error": "mode must be one of ok, slow, timeout, error (alias: fail)"}
        )
    state.mode = mode
    state.delay_ms = delay_ms if delay_ms is not None else DEFAULT_SLOW_DELAY_MS
    emit("mode_changed", mode=mode, delay_ms=state.delay_ms)
    return JSONResponse(content={"mode": mode, "delay_ms": state.delay_ms})


@app.get("/_stats")
async def stats() -> dict:
    return {"calls": dict(state.calls), "scored": dict(state.scored), "callback_sends": dict(state.callback_sends)}


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
