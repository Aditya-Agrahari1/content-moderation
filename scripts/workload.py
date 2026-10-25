"""Repeatable two-workspace workload: normal -> provider outage -> recovery.

  ws_verified   (A, burst):   200 videos at the start of phase 1 and again at the start of the outage.
  ws_unverified (B, trickle): one image per --image-interval seconds through every phase, plus
                              (during the outage) 5 incident videos with an explicit last frame,
                              5 prohibited generation prompts and 5 safe ones.

Everything is measured during the run and written to --out. Uses only the HTTP API, provider
/_control + /_stats and database columns that exist at both `broken-baseline` and the fix.

Usage (inside the api container, stack started with docker-compose.test.yml):
  python scripts/workload.py --out evidence/after/workload.json [--video-frames 40]
"""

import argparse
import asyncio
import json
import os
import random
import statistics
import sys
import time

import asyncpg
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from scripts.audit import run_audit  # noqa: E402
from tests.helpers.client import Api  # noqa: E402
from tests.helpers.policy_oracle import expected_decision  # noqa: E402

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@db:5432/moderation")
VERIFIED = {"ws_verified": True, "ws_unverified": False}
BAD_PROMPTS = [
    "explicit photo of a 16-year-old girl",
    "nude s1xteen year old",
    "sexy schoolgirl, underage",
    "a brother and sister having sex (incest)",
    "trafficked girls forced into sex work",
]
SAFE_PROMPTS = [
    "two adults, both 25, on a beach at sunset",
    "a 16 oz coffee on a wooden table",
    "a minor chord progression drawn as abstract art",
    "an eighteen year old couple at their graduation",
    "a nineteen-year-old woman hiking",
]
# Shared-column query: unresolved = anything not finished, in either schema's job vocabulary.
OLDEST_UNRESOLVED_SQL = (
    "SELECT workspace_id, EXTRACT(EPOCH FROM now() - MIN(created_at))::float AS age, COUNT(*) AS n "
    "FROM screening_jobs WHERE status NOT IN ('done', 'completed', 'failed', 'cancelled') GROUP BY workspace_id"
)


class Run:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rng = random.Random(args.seed)
        self.t0 = time.monotonic()
        self.phase = "normal"
        self.media: dict[str, dict] = {}  # id -> record
        self.prompts: list[dict] = []
        self.timeline: list[dict] = []
        self.stop_trickle = asyncio.Event()

    def now(self) -> float:
        return round(time.monotonic() - self.t0, 2)

    def track(self, media_id: str, workspace: str, kind: str, frames: list[dict]) -> None:
        self.media[media_id] = {
            "workspace": workspace, "kind": kind, "phase": self.phase, "accepted_at": self.now(),
            "expected": expected_decision(frames, VERIFIED[workspace]), "left_pending_at": None,
            "decided_at": None, "final_status": "pending", "hold_reason": None,
        }


def a_video(rng: random.Random, n_frames: int) -> list[dict]:
    frames = [{"explicit": round(rng.uniform(0, 0.3), 3)} for _ in range(n_frames)]
    roll = rng.random()
    if roll < 0.05:
        frames[min(15, n_frames - 1)] = {"minor": 0.95}  # late prohibited frame -> blocked
    elif roll < 0.10:
        frames[-1] = {"self_harm": 0.6}  # review band -> held
    elif roll < 0.30:
        frames[rng.randrange(n_frames)] = {"explicit": 0.95}  # explicit, verified workspace -> published
    return frames


async def burst(run: Run, api: Api, count: int) -> None:
    semaphore = asyncio.Semaphore(25)

    async def one() -> None:
        frames = a_video(run.rng, run.args.video_frames)
        async with semaphore:
            response = await api.upload("member_v", {"type": "video", "source": "upload", "frames": frames})
        if response.status_code == 202:
            run.track(response.json()["id"], "ws_verified", "burst_video", frames)

    await asyncio.gather(*(one() for _ in range(count)))


async def trickle(run: Run, api: Api) -> None:
    while not run.stop_trickle.is_set():
        frames = [{"explicit": 0.95 if run.rng.random() < 0.2 else 0.02}]
        response = await api.upload("member_u", {"type": "image", "source": "upload", "frames": frames})
        if response.status_code == 202:
            run.track(response.json()["id"], "ws_unverified", "trickle_image", frames)
        try:
            await asyncio.wait_for(run.stop_trickle.wait(), timeout=run.args.image_interval)
        except TimeoutError:
            pass


async def outage_extras(run: Run, api: Api) -> None:
    for _ in range(5):
        frames = [{"explicit": 0.02} for _ in range(run.args.video_frames)]
        frames[-1] = {"explicit": 0.95}  # the incident video: explicit content at the very end
        media_id = await api.upload_ok("member_u", {"type": "video", "source": "upload", "frames": frames})
        run.track(media_id, "ws_unverified", "incident_video", frames)
    for prompt, prohibited in [(p, True) for p in BAD_PROMPTS] + [(p, False) for p in SAFE_PROMPTS]:
        body = {"type": "image", "source": "generation", "prompt": prompt, "frames": [{"explicit": 0.1}]}
        response = await api.upload("member_u", body)
        run.prompts.append({"prohibited": prohibited, "http_status": response.status_code})
        if response.status_code == 202:
            run.track(response.json()["id"], "ws_unverified", "generation", body["frames"])


def decided(status: str, hold_reason: str | None) -> bool:
    """Final screening outcome: published/blocked, or held for a human (not held for an outage)."""
    return status in ("published", "blocked") or (status == "held" and hold_reason in (None, "needs_human_review"))


async def sampler(run: Run, conn: asyncpg.Connection, stop: asyncio.Event) -> None:
    while not stop.is_set():
        ages = {r["workspace_id"]: {"oldest_s": round(r["age"], 1), "unresolved": r["n"]}
                for r in await conn.fetch(OLDEST_UNRESOLVED_SQL)}
        ids = list(run.media)
        rows = await conn.fetch(
            "SELECT id::text AS id, status, to_jsonb(m) ->> 'hold_reason' AS hold_reason "
            "FROM media m WHERE id = ANY($1::uuid[])", ids,
        ) if ids else []
        now = run.now()
        counts: dict[str, dict[str, int]] = {}
        for row in rows:
            record = run.media[row["id"]]
            record["final_status"], record["hold_reason"] = row["status"], row["hold_reason"]
            if row["status"] != "pending" and record["left_pending_at"] is None:
                record["left_pending_at"] = now
            if decided(row["status"], row["hold_reason"]) and record["decided_at"] is None:
                record["decided_at"] = now
            label = row["status"] if row["status"] != "held" else f"held:{row['hold_reason']}"
            counts.setdefault(record["workspace"], {}).setdefault(label, 0)
            counts[record["workspace"]][label] += 1
        run.timeline.append({"t": now, "phase": run.phase, "jobs": ages, "media": counts})
        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except TimeoutError:
            pass


def summarize_latency(values: list[float]) -> dict:
    if not values:
        return {"n": 0}
    values = sorted(values)
    p95 = values[min(len(values) - 1, int(round(0.95 * (len(values) - 1))))]
    return {"n": len(values), "p50": round(statistics.median(values), 2), "p95": round(p95, 2),
            "max": round(values[-1], 2)}


def report(run: Run, stats: dict, audit: dict) -> dict:
    by_group: dict[str, dict] = {}
    for record in run.media.values():
        key = f"{record['workspace']}/{record['phase']}"
        group = by_group.setdefault(key, {"left_pending": [], "decided": [], "count": 0, "undecided": 0})
        group["count"] += 1
        if record["left_pending_at"] is not None:
            group["left_pending"].append(record["left_pending_at"] - record["accepted_at"])
        if record["decided_at"] is not None:
            group["decided"].append(record["decided_at"] - record["accepted_at"])
        else:
            group["undecided"] += 1
    latency = {key: {"count": g["count"], "undecided_at_end": g["undecided"],
                     "time_to_leave_pending_s": summarize_latency(g["left_pending"]),
                     "time_to_decision_s": summarize_latency(g["decided"])} for key, g in by_group.items()}

    ids = list(run.media)
    calls = [stats["calls"].get(i, 0) for i in ids]
    scored = [stats.get("scored", {}).get(i, 0) for i in ids]
    oldest: dict[str, dict[str, float]] = {}
    for point in run.timeline:
        for ws, value in point["jobs"].items():
            slot = oldest.setdefault(point["phase"], {})
            slot[ws] = max(slot.get(ws, 0.0), value["oldest_s"])
    recovery_start = next((p["t"] for p in run.timeline if p["phase"] == "recovery"), None)
    drained_at = next((p["t"] for p in run.timeline if p["phase"] == "recovery" and not p["jobs"]), None)
    wrong = [r for r in run.media.values() if decided(r["final_status"], r["hold_reason"])
             and r["final_status"] != r["expected"]]
    return {
        "params": vars(run.args),
        "uploads_accepted": len(ids),
        "published_unchecked": audit["published_unchecked"],
        "audit": audit,
        "provider_calls_total": sum(calls),
        "provider_calls_per_upload": round(sum(calls) / len(ids), 3) if ids else None,
        "provider_scored_per_upload": round(sum(scored) / len(ids), 3) if ids else None,
        "max_calls_for_one_media": max(calls, default=0),
        "media_scored_more_than_once": sum(1 for s in scored if s > 1),
        "prohibited_prompts_accepted": sum(1 for p in run.prompts if p["prohibited"] and p["http_status"] == 202),
        "safe_prompts_refused": sum(1 for p in run.prompts if not p["prohibited"] and p["http_status"] != 202),
        "final_decision_differs_from_oracle": len(wrong),
        "oldest_unresolved_job_age_max_s": oldest,
        "backlog_drained_s_after_recovery": (round(drained_at - recovery_start, 1)
                                             if drained_at is not None and recovery_start is not None else None),
        "final_status_counts": run.timeline[-1]["media"] if run.timeline else {},
        "latency_by_workspace_phase": latency,
        "timeline": run.timeline,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--burst", type=int, default=200)
    parser.add_argument("--video-frames", type=int, default=20)
    parser.add_argument("--image-interval", type=float, default=1.0)
    parser.add_argument("--normal-s", type=float, default=20)
    parser.add_argument("--outage-s", type=float, default=60)
    parser.add_argument("--recovery-max-s", type=float, default=180)
    parser.add_argument("--outage-mode", default="timeout")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    run = Run(args)
    conn = await asyncpg.connect(DATABASE_URL)
    stop_sampler = asyncio.Event()
    async with httpx.AsyncClient(timeout=30) as http:
        api = Api(http)
        await api.set_mode("ok")
        sampling = asyncio.create_task(sampler(run, conn, stop_sampler))
        trickling = asyncio.create_task(trickle(run, api))

        print(f"[{run.now()}] phase normal: burst of {args.burst} x {args.video_frames}-frame videos", flush=True)
        await burst(run, api, args.burst)
        await asyncio.sleep(args.normal_s)

        run.phase = "outage"
        await api.set_mode(args.outage_mode)
        print(f"[{run.now()}] phase outage ({args.outage_mode}): burst of {args.burst}", flush=True)
        await burst(run, api, args.burst)
        await outage_extras(run, api)
        await asyncio.sleep(args.outage_s)

        run.phase = "recovery"
        await api.set_mode("ok")
        print(f"[{run.now()}] phase recovery: waiting for every upload to be decided", flush=True)
        deadline = time.monotonic() + args.recovery_max_s
        while time.monotonic() < deadline:
            if run.media and all(r["decided_at"] is not None for r in run.media.values()) and run.now() > 0:
                break
            await asyncio.sleep(1)
        run.stop_trickle.set()
        await trickling
        await asyncio.sleep(3)  # let the trickle's last uploads and any duplicate calls land
        stop_sampler.set()
        await sampling

        stats = await api.stats()
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    audit = await run_audit(conn, stats, list(run.media))
    await conn.close()

    result = report(run, stats, audit)
    with open(args.out, "w") as fh:
        json.dump(result, fh, indent=2)
    summary = {k: v for k, v in result.items() if k not in ("timeline", "params")}
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
