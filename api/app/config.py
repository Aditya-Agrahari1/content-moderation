"""All configuration comes from environment variables (documented in .env.example)."""

import os
from dataclasses import dataclass, field

PLACEHOLDER_SECRET = "local-dev-placeholder-not-secret"


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, default))


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "postgresql://app:app@db:5432/moderation"))
    redis_url: str = field(default_factory=lambda: os.getenv("REDIS_URL", "redis://redis:6379/0"))
    provider_url: str = field(default_factory=lambda: os.getenv("PROVIDER_URL", "http://provider:8001"))
    callback_url: str = field(default_factory=lambda: os.getenv("CALLBACK_URL", "http://api:8000/provider/callback"))
    callback_secret: str = field(default_factory=lambda: os.getenv("PROVIDER_CALLBACK_SECRET", PLACEHOLDER_SECRET))
    frontend_origin: str = field(default_factory=lambda: os.getenv("FRONTEND_ORIGIN", "http://localhost:3000"))

    provider_connect_timeout_s: float = field(default_factory=lambda: _float("PROVIDER_CONNECT_TIMEOUT_S", 2))
    provider_read_timeout_s: float = field(default_factory=lambda: _float("PROVIDER_READ_TIMEOUT_S", 10))

    worker_concurrency: int = field(default_factory=lambda: _int("WORKER_CONCURRENCY", 4))
    worker_poll_interval_s: float = field(default_factory=lambda: _float("WORKER_POLL_INTERVAL_S", 2))
    max_concurrent_per_workspace: int = field(default_factory=lambda: _int("MAX_CONCURRENT_PER_WORKSPACE", 4))
    lease_duration_s: float = field(default_factory=lambda: _float("LEASE_DURATION_S", 30))
    lease_heartbeat_s: float = field(default_factory=lambda: _float("LEASE_HEARTBEAT_S", 5))

    max_failures_per_cycle: int = field(default_factory=lambda: _int("MAX_FAILURES_PER_CYCLE", 4))
    max_rate_limited_per_cycle: int = field(default_factory=lambda: _int("MAX_RATE_LIMITED_PER_CYCLE", 10))
    max_malformed_total: int = field(default_factory=lambda: _int("MAX_MALFORMED_TOTAL", 3))
    backoff_base_s: float = field(default_factory=lambda: _float("BACKOFF_BASE_S", 1))
    backoff_cap_s: float = field(default_factory=lambda: _float("BACKOFF_CAP_S", 8))
    retry_after_default_s: float = field(default_factory=lambda: _float("RETRY_AFTER_DEFAULT_S", 10))
    retry_after_max_s: float = field(default_factory=lambda: _float("RETRY_AFTER_MAX_S", 60))

    cb_failure_threshold: int = field(default_factory=lambda: _int("CB_FAILURE_THRESHOLD", 5))
    cb_cooldown_s: float = field(default_factory=lambda: _float("CB_COOLDOWN_S", 10))
    cb_cooldown_max_s: float = field(default_factory=lambda: _float("CB_COOLDOWN_MAX_S", 60))

    sweep_interval_s: float = field(default_factory=lambda: _float("SWEEP_INTERVAL_S", 2))
    held_provider_min_wait_s: float = field(default_factory=lambda: _float("HELD_PROVIDER_MIN_WAIT_S", 30))
    callback_deadline_s: float = field(default_factory=lambda: _float("CALLBACK_DEADLINE_S", 60))

    max_frame_count: int = field(default_factory=lambda: _int("MAX_FRAME_COUNT", 3600))
    max_body_bytes: int = field(default_factory=lambda: _int("MAX_BODY_BYTES", 1_048_576))


settings = Settings()
