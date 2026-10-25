"""Audit: count media that went live without a valid screening.

Does not trust job status or the app's own flags. Runs at both `broken-baseline` and the fix.
  Check A - provider evidence: the fake provider never scored a published item (/_stats).
  Check B - policy oracle: the original input frames say the item should be held or blocked
            (admin-approved holds are legitimate and excluded).
  Check C - fixed schema only: a published item has no screening_results row covering every frame.

Usage (inside the api container):  python scripts/audit.py [--out evidence/after/audit.json]
"""

import argparse
import asyncio
import json
import os
import sys

import asyncpg
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tests.helpers.policy_oracle import expected_decision  # noqa: E402

PROVIDER_URL = os.getenv("PROVIDER_URL", "http://provider:8001")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://app:app@db:5432/moderation")


async def _jsonb(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def run_audit(conn: asyncpg.Connection, stats: dict, media_ids: list[str] | None = None) -> dict:
    rows = await conn.fetch(
        "SELECT m.id::text AS id, m.workspace_id, m.frames, m.reasons, w.verified "
        "FROM media m JOIN workspaces w ON w.id = m.workspace_id "
        "WHERE m.status = 'published' AND ($1::uuid[] IS NULL OR m.id = ANY($1::uuid[]))",
        media_ids,
    )
    scored = stats.get("scored", {})
    calls = stats.get("calls", {})
    check_a, check_b, check_c = [], [], []
    for row in rows:
        if scored.get(row["id"], 0) == 0:
            check_a.append({"id": row["id"], "workspace_id": row["workspace_id"], "provider_calls": calls.get(row["id"], 0)})
        expected = expected_decision(row["frames"], row["verified"])
        approved_hold = expected == "held" and "admin_approved" in row["reasons"]
        if expected != "published" and not approved_hold:
            check_b.append({"id": row["id"], "workspace_id": row["workspace_id"], "expected": expected})

    has_results = await conn.fetchval("SELECT to_regclass('screening_results') IS NOT NULL")
    if has_results:
        bad = await conn.fetch(
            "SELECT m.id::text AS id, m.workspace_id FROM media m "
            "LEFT JOIN screening_results r ON r.media_id = m.id "
            "WHERE m.status = 'published' AND ($1::uuid[] IS NULL OR m.id = ANY($1::uuid[])) "
            "AND (r.media_id IS NULL OR r.frames_scored <> m.frame_count "
            "     OR NOT (r.decision = 'published' OR (r.decision = 'held' AND EXISTS ("
            "         SELECT 1 FROM media_reviews v WHERE v.media_id = m.id AND v.action = 'approve'))))",
            media_ids,
        )
        check_c = [dict(r) for r in bad]

    unchecked = {r["id"] for r in check_a} | {r["id"] for r in check_b} | {r["id"] for r in check_c}
    return {
        "published_total": len(rows),
        "published_unchecked": len(unchecked),
        "check_a_published_without_provider_score": len(check_a),
        "check_b_published_against_policy": len(check_b),
        "check_c_published_without_covering_result": len(check_c) if has_results else "skipped (no screening_results table)",
        "examples": {"a": check_a[:5], "b": check_b[:5], "c": check_c[:5]},
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    args = parser.parse_args()
    async with httpx.AsyncClient(timeout=10) as http:
        stats = (await http.get(f"{PROVIDER_URL}/_stats")).json()
    conn = await asyncpg.connect(DATABASE_URL)
    await _jsonb(conn)
    try:
        result = await run_audit(conn, stats)
    finally:
        await conn.close()
    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")


if __name__ == "__main__":
    asyncio.run(main())
