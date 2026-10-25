"""Screening worker: WORKER_CONCURRENCY async tasks claiming jobs, plus the sweeper (one leader
across all worker containers). Stops cleanly on SIGTERM: tasks finish their current job; if the
process is killed instead, leases expire and the sweeper re-queues the work."""

import asyncio
import signal

import httpx

from app import coord, db, log, wakeup
from app.circuit import Circuit
from app.config import settings
from app.worker import claim, process, sweeper


async def worker_task(index: int, http: httpx.AsyncClient, circuit: Circuit, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            async with db.pool().acquire() as conn:
                job = await claim.claim_one(conn)
            if job is None:
                await wakeup.wait(settings.worker_poll_interval_s)
                continue
            log.info("job_claimed", task=index, job_id=job["id"], media_id=job["media_id"],
                     workspace_id=job["workspace_id"], cycle=job["cycle"], lease_token=job["lease_token"])
            await process.process(dict(job), http, circuit)
        except Exception as exc:  # the job stays in the database; its lease expires and it is retried
            log.error("worker_error", task=index, error=type(exc).__name__, detail=str(exc)[:300])
            await asyncio.sleep(1)


async def run() -> None:
    log.configure("worker")
    await db.open_pool(max_size=settings.worker_concurrency + 3)
    circuit = Circuit(coord.store())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    log.info("worker_started", concurrency=settings.worker_concurrency,
             max_concurrent_per_workspace=settings.max_concurrent_per_workspace)
    timeout = httpx.Timeout(settings.provider_read_timeout_s, connect=settings.provider_connect_timeout_s)
    async with httpx.AsyncClient(timeout=timeout) as http:
        await asyncio.gather(
            sweeper.run_sweeper(circuit, stop),
            *(worker_task(i, http, circuit, stop) for i in range(settings.worker_concurrency)),
        )
    await db.close_pool()
    log.info("worker_stopped")
