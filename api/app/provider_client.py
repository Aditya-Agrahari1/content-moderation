"""Provider client: one HTTP call, classified into exactly one outcome. Never raises for HTTP trouble.

A response is only `success` when it scores *every* frame with *every* label; anything else is
`malformed` and is never treated as a decision.
"""

import math
import time
from dataclasses import dataclass

import httpx

from app.config import settings
from app.policy import ALL_LABELS

SUCCESS = "success"
ASYNC_ACCEPTED = "async_accepted"
TIMEOUT = "timeout"
CONN_ERROR = "conn_error"
SERVER_ERROR = "server_error"
RATE_LIMITED = "rate_limited"
IN_PROGRESS = "in_progress"
MALFORMED = "malformed"
CLIENT_ERROR = "client_error"

RETRYABLE_FAILURES = {TIMEOUT, CONN_ERROR, SERVER_ERROR, MALFORMED}  # count toward the cycle budget
PUSHBACK = {RATE_LIMITED, IN_PROGRESS}  # provider healthy but asking us to wait


@dataclass(frozen=True)
class Outcome:
    kind: str
    http_status: int | None = None
    frames: list[dict[str, float]] | None = None
    request_id: str | None = None
    retry_after_s: float | None = None
    latency_ms: int = 0


def _is_score(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 <= value <= 1


def validate_frames(frames: object, expected_count: int) -> list[dict[str, float]] | None:
    """Canonical frames (all six labels, numbers in [0, 1], one per input frame) or None."""
    if not isinstance(frames, list) or len(frames) != expected_count:
        return None
    canonical = []
    for frame in frames:
        if not isinstance(frame, dict) or set(frame) != set(ALL_LABELS):
            return None
        if not all(_is_score(frame[label]) for label in ALL_LABELS):
            return None
        canonical.append({label: float(frame[label]) for label in ALL_LABELS})
    return canonical


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return max(0.0, float(response.headers["Retry-After"]))
    except (KeyError, ValueError):
        return None


def classify(response: httpx.Response, media_id: str, expected_frames: int, latency_ms: int = 0) -> Outcome:
    status = response.status_code
    if status in (200, 202):
        try:
            body = response.json()
        except ValueError:
            return Outcome(MALFORMED, status, latency_ms=latency_ms)
        if not isinstance(body, dict) or body.get("media_id") != media_id or not isinstance(body.get("request_id"), str):
            return Outcome(MALFORMED, status, latency_ms=latency_ms)
        if status == 202:
            if body.get("status") != "accepted":
                return Outcome(MALFORMED, status, latency_ms=latency_ms)
            return Outcome(ASYNC_ACCEPTED, status, request_id=body["request_id"], latency_ms=latency_ms)
        frames = validate_frames(body.get("frames"), expected_frames)
        if frames is None:
            return Outcome(MALFORMED, status, latency_ms=latency_ms)
        return Outcome(SUCCESS, status, frames=frames, request_id=body["request_id"], latency_ms=latency_ms)
    if status == 429:
        return Outcome(RATE_LIMITED, status, retry_after_s=_retry_after(response), latency_ms=latency_ms)
    if status == 409:
        return Outcome(IN_PROGRESS, status, retry_after_s=_retry_after(response), latency_ms=latency_ms)
    if status >= 500:
        return Outcome(SERVER_ERROR, status, latency_ms=latency_ms)
    return Outcome(CLIENT_ERROR, status, latency_ms=latency_ms)


async def score(http: httpx.AsyncClient, media_id: str, frames: list[dict]) -> Outcome:
    """POST /score with the media ID as Idempotency-Key, so retries can be answered from the
    provider's stored result instead of being scored (and billed) again."""
    started = time.monotonic()
    try:
        response = await http.post(
            f"{settings.provider_url}/score",
            json={"media_id": media_id, "frames": frames, "callback_url": settings.callback_url},
            headers={"Idempotency-Key": media_id},
        )
    except httpx.TimeoutException:
        return Outcome(TIMEOUT, latency_ms=int((time.monotonic() - started) * 1000))
    except httpx.TransportError:
        return Outcome(CONN_ERROR, latency_ms=int((time.monotonic() - started) * 1000))
    return classify(response, media_id, len(frames), int((time.monotonic() - started) * 1000))
