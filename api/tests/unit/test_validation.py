import httpx
import pytest
from pydantic import ValidationError

from app import provider_client as pc
from app.schemas import MediaIn

FULL = {"minor": 0, "sexual_violence": 0, "bestiality": 0, "self_harm": 0, "real_person": 0, "explicit": 0.1}


def media(frames, **extra):
    return {"type": "video", "source": "upload", "frames": frames, **extra}


@pytest.mark.parametrize(
    "frames",
    [
        [{"explicit": float("nan")}],
        [{"explicit": float("inf")}],
        [{"explicit": True}],
        [{"explicit": "0.1"}],
        [{"explicit": -0.1}],
        [{"explicit": 1.01}],
        [{"nudity": 0.1}],  # unknown label
        [],
    ],
)
def test_bad_media_input_rejected(frames):
    with pytest.raises(ValidationError):
        MediaIn.model_validate(media(frames))


def test_media_shape_rules():
    with pytest.raises(ValidationError):
        MediaIn.model_validate({"type": "image", "source": "upload", "frames": [{}, {}]})
    with pytest.raises(ValidationError):
        MediaIn.model_validate({"type": "image", "source": "generation", "frames": [{}]})
    assert MediaIn.model_validate(media([{}, {"explicit": 1}])).frames[1] == {"explicit": 1}


def test_validate_frames_requires_every_label_and_frame():
    assert pc.validate_frames([FULL, FULL], 2) == [{k: float(v) for k, v in FULL.items()}] * 2
    assert pc.validate_frames([FULL], 2) is None  # partial coverage
    assert pc.validate_frames([{k: v for k, v in FULL.items() if k != "minor"}], 1) is None  # missing label
    assert pc.validate_frames([{**FULL, "minor": True}], 1) is None
    assert pc.validate_frames([{**FULL, "minor": 2}], 1) is None
    assert pc.validate_frames([{**FULL, "extra": 0}], 1) is None
    assert pc.validate_frames("nope", 1) is None


def response(status, body=None, headers=None, text=None):
    if text is not None:
        return httpx.Response(status, text=text, headers=headers)
    return httpx.Response(status, json=body, headers=headers)


@pytest.mark.parametrize(
    "resp,kind",
    [
        (response(200, {"media_id": "m", "request_id": "r", "frames": [FULL]}), pc.SUCCESS),
        (response(202, {"media_id": "m", "request_id": "r", "status": "accepted"}), pc.ASYNC_ACCEPTED),
        (response(200, {"media_id": "m", "request_id": "r", "frames": []}), pc.MALFORMED),
        (response(200, {"media_id": "other", "request_id": "r", "frames": [FULL]}), pc.MALFORMED),
        (response(200, text="<html>oops</html>"), pc.MALFORMED),
        (response(202, {"media_id": "m"}), pc.MALFORMED),
        (response(429, {}, {"Retry-After": "7"}), pc.RATE_LIMITED),
        (response(409, {"status": "in_progress"}, {"Retry-After": "1"}), pc.IN_PROGRESS),
        (response(500, {}), pc.SERVER_ERROR),
        (response(503, {}), pc.SERVER_ERROR),
        (response(422, {"error": "idempotency_key_reused"}), pc.CLIENT_ERROR),
        (response(400, {}), pc.CLIENT_ERROR),
    ],
)
def test_classify(resp, kind):
    assert pc.classify(resp, "m", 1).kind == kind


def test_retry_after_parsed():
    assert pc.classify(response(429, {}, {"Retry-After": "7"}), "m", 1).retry_after_s == 7.0
    assert pc.classify(response(429, {}), "m", 1).retry_after_s is None
    assert pc.classify(response(429, {}, {"Retry-After": "soon"}), "m", 1).retry_after_s is None


async def test_score_maps_transport_errors():
    def raise_timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    def raise_connect(request):
        raise httpx.ConnectError("refused", request=request)

    for handler, kind in [(raise_timeout, pc.TIMEOUT), (raise_connect, pc.CONN_ERROR)]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            assert (await pc.score(http, "m", [{}])).kind == kind


async def test_score_sends_idempotency_key():
    seen = {}

    def handler(request):
        seen["key"] = request.headers.get("Idempotency-Key")
        return httpx.Response(200, json={"media_id": "m", "request_id": "r", "frames": [FULL]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert (await pc.score(http, "m", [{}])).kind == pc.SUCCESS
    assert seen["key"] == "m"
