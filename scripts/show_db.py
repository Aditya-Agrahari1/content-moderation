"""Show what PostgreSQL recorded: the proof behind the demo.

    python scripts/show_db.py              latest media items + table counts
    python scripts/show_db.py <media_id>   everything stored about one item
    python scripts/show_db.py guard        try to publish unchecked media directly in SQL (the database refuses)
"""

import subprocess
import sys

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.test.yml"]


def psql(sql: str, title: str) -> None:
    print(f"\n=== {title} ===")
    result = subprocess.run([*COMPOSE, "exec", "-T", "db", "psql", "-U", "app", "-d", "moderation", "-P", "pager=off",
                             "-c", sql], capture_output=True, text=True, encoding="utf-8", errors="replace")
    print((result.stdout + result.stderr).rstrip())


def overview() -> None:
    psql("SELECT left(id::text, 8) AS media, workspace_id, type, source, status, hold_reason, reasons, "
         "to_char(created_at, 'HH24:MI:SS') AS created FROM media ORDER BY created_at DESC LIMIT 10",
         "media: the 10 most recent uploads")
    psql("SELECT (SELECT count(*) FROM media) AS media, (SELECT count(*) FROM screening_jobs) AS jobs, "
         "(SELECT count(*) FROM job_attempts) AS provider_calls, (SELECT count(*) FROM screening_results) AS results, "
         "(SELECT count(*) FROM media_reviews) AS reviews", "row counts")
    psql("SELECT m.status, count(*) AS media, count(r.media_id) AS with_complete_result "
         "FROM media m LEFT JOIN screening_results r ON r.media_id = m.id AND r.frames_scored = m.frame_count "
         "GROUP BY m.status ORDER BY 1",
         "every published item has a complete screening result (published = with_complete_result)")


def one(media_id: str) -> None:
    where = f"'{media_id}'::uuid" if len(media_id) == 36 else \
        f"(SELECT id FROM media WHERE id::text LIKE '{media_id}%' LIMIT 1)"
    psql(f"SELECT id, workspace_id, type, status, hold_reason, reasons, frame_count FROM media WHERE id = {where}",
         "media")
    psql(f"SELECT status, attempt AS provider_calls, cycle, cycle_failures, last_error_class, lease_token "
         f"FROM screening_jobs WHERE media_id = {where}", "screening_jobs (one job, reused for every retry)")
    psql(f"SELECT a.attempt_number, a.cycle, left(a.lease_token::text, 8) AS lease, a.outcome, a.http_status, "
         f"a.latency_ms, to_char(a.started_at, 'HH24:MI:SS.MS') AS started "
         f"FROM job_attempts a JOIN screening_jobs j ON j.id = a.job_id WHERE j.media_id = {where} "
         f"ORDER BY a.attempt_number", "job_attempts (one row per provider call)")
    psql(f"SELECT decision, frames_scored, reasons, provider_request_id, to_char(created_at, 'HH24:MI:SS') AS at "
         f"FROM screening_results WHERE media_id = {where}", "screening_results (at most one per media)")
    psql(f"SELECT user_id, action, to_char(created_at, 'HH24:MI:SS') AS at FROM media_reviews WHERE media_id = {where}",
         "media_reviews (admin decisions)")


def guard() -> None:
    print("Creating a media item directly in SQL, then trying to publish it without any screening result...")
    # Explicit transaction that is never committed: nothing from this demo is left in the database.
    psql("BEGIN; INSERT INTO media (workspace_id, user_id, type, source, frames) "
         "VALUES ('ws_unverified', 'member_u', 'image', 'upload', '[{\"explicit\": 0.99}]'); "
         "UPDATE media SET status = 'published' WHERE id = (SELECT id FROM media WHERE user_id = 'member_u' "
         "ORDER BY created_at DESC LIMIT 1); ROLLBACK;",
         "UPDATE media SET status = 'published' (no screening result)")
    psql("UPDATE screening_results SET decision = 'published' WHERE media_id = (SELECT media_id FROM screening_results LIMIT 1)",
         "UPDATE screening_results (rewrite a stored decision)")
    print("\nBoth refused by the database itself, even when the application is bypassed.")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else None
    if arg is None:
        overview()
    elif arg == "guard":
        guard()
    else:
        one(arg)
