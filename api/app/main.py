import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app import db, log
from app.config import PLACEHOLDER_SECRET, settings
from app.routes import callback, media, review


@asynccontextmanager
async def lifespan(_: FastAPI):
    log.configure("api")
    if settings.callback_secret == PLACEHOLDER_SECRET:
        log.warning("placeholder_callback_secret", detail="set PROVIDER_CALLBACK_SECRET outside local development")
    await db.open_pool()
    yield
    await db.close_pool()


app = FastAPI(title="moderation-api", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.frontend_origin],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-User-Id", "X-Workspace-Id"],
)
app.include_router(media.router)
app.include_router(review.router)
app.include_router(callback.router)


@app.middleware("http")
async def request_id(request: Request, call_next):
    token = log.request_id_var.set(request.headers.get("X-Request-Id") or uuid.uuid4().hex)
    try:
        return await call_next(request)
    finally:
        log.request_id_var.reset(token)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    reasons = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err["loc"] if part != "body")
        reasons.append(f"{location}: {err['msg']}" if location else err["msg"])
    return JSONResponse(status_code=422, content={"reasons": reasons})


@app.exception_handler(Exception)
async def unhandled_error(_: Request, exc: Exception) -> JSONResponse:
    log.error("unhandled_error", error=type(exc).__name__)
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.get("/readyz")
async def readyz() -> JSONResponse:
    try:
        async with db.pool().acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception:  # readiness only reports; it never changes data
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(content={"status": "ready"})
