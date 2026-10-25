"""Provider circuit breaker shared by all workers, plus the global 429 pause.

closed     calls go through; CB_FAILURE_THRESHOLD consecutive failures open it.
open       no calls until `open_until`; workers skip jobs without using an attempt.
half_open  `open_until` has passed: exactly one worker wins the canary lock and makes one call.
           Success closes the circuit. Failure re-opens it with a doubled cooldown (capped).
"""

import time

from app import log
from app.config import settings
from app.coord import Store

FAILURES = "cb:failures"
OPEN_UNTIL = "cb:open_until"
COOLDOWN = "cb:cooldown_s"
CANARY = "cb:canary"
PAUSE_UNTIL = "provider:pause_until"


class Circuit:
    def __init__(self, store: Store) -> None:
        self.store = store

    async def open_until(self) -> float | None:
        value = await self.store.get(OPEN_UNTIL)
        return float(value) if value else None

    async def state(self) -> str:
        open_until = await self.open_until()
        if open_until is None:
            return "closed"
        return "open" if time.time() < open_until else "half_open"

    async def allow_call(self) -> tuple[bool, bool]:
        """(allowed, is_canary)."""
        open_until = await self.open_until()
        if open_until is None:
            return True, False
        if time.time() < open_until:
            return False, False
        canary_ttl = settings.provider_read_timeout_s + settings.provider_connect_timeout_s + 5
        if await self.store.set(CANARY, "1", ttl_s=canary_ttl, only_if_absent=True):
            log.info("circuit_half_open", detail="canary call allowed")
            return True, True
        return False, False

    async def record_success(self, is_canary: bool) -> None:
        was_open = await self.open_until() is not None
        await self.store.delete(FAILURES, OPEN_UNTIL, COOLDOWN, CANARY)
        if was_open:
            log.info("circuit_closed", canary=is_canary)

    async def record_failure(self, is_canary: bool) -> None:
        if is_canary:
            cooldown = min(float(await self.store.get(COOLDOWN) or settings.cb_cooldown_s) * 2, settings.cb_cooldown_max_s)
            await self._open(cooldown)
            await self.store.delete(CANARY)
            log.warning("circuit_reopened", cooldown_s=cooldown)
            return
        failures = await self.store.incr(FAILURES, ttl_s=max(30.0, settings.cb_cooldown_max_s))
        if failures >= settings.cb_failure_threshold and await self.open_until() is None:
            await self._open(settings.cb_cooldown_s)
            log.warning("circuit_opened", consecutive_failures=failures, cooldown_s=settings.cb_cooldown_s)

    async def release_canary(self) -> None:
        """A canary that got a neutral answer (429/409/4xx) proves nothing either way."""
        await self.store.delete(CANARY)

    async def _open(self, cooldown_s: float) -> None:
        await self.store.set(COOLDOWN, str(cooldown_s))
        await self.store.set(OPEN_UNTIL, str(time.time() + cooldown_s))

    async def pause(self, seconds: float) -> None:
        """Honour Retry-After for every worker (429 does not open the circuit)."""
        until = time.time() + seconds
        await self.store.set(PAUSE_UNTIL, str(until), ttl_s=seconds)
        log.warning("provider_paused", retry_after_s=round(seconds, 2))

    async def paused_until(self) -> float | None:
        value = await self.store.get(PAUSE_UNTIL)
        return float(value) if value and float(value) > time.time() else None
