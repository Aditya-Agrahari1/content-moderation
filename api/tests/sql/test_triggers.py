"""Database publication guard (migration 0002), exercised with direct SQL against PostgreSQL 16.

Media rows created here have no screening job, so the running workers never touch them.
"""

import asyncpg
import pytest

FULL = {"minor": 0.0, "sexual_violence": 0.0, "bestiality": 0.0, "self_harm": 0.0, "real_person": 0.0, "explicit": 0.0}


async def new_media(conn, n_frames: int = 1, workspace: str = "ws_verified", user: str = "member_v"):
    kind = "image" if n_frames == 1 else "video"
    return await conn.fetchval(
        "INSERT INTO media (workspace_id, user_id, type, source, frames) VALUES ($1, $2, $3, 'upload', $4) RETURNING id",
        workspace, user, kind, [{}] * n_frames,
    )


async def add_result(conn, media_id, decision: str, n_frames: int = 1):
    # screening_results.job_id must reference a job; use a completed one so no worker picks it up.
    job_id = await conn.fetchval(
        "INSERT INTO screening_jobs (media_id, workspace_id, status) "
        "SELECT id, workspace_id, 'completed' FROM media WHERE id = $1 RETURNING id",
        media_id,
    )
    await conn.execute(
        "INSERT INTO screening_results (media_id, job_id, raw_response, decision, reasons) VALUES ($1, $2, $3, $4, '[]')",
        media_id, job_id, {"frames": [FULL] * n_frames}, decision,
    )


async def set_status(conn, media_id, status, hold_reason=None):
    await conn.execute("UPDATE media SET status = $2, hold_reason = $3 WHERE id = $1", media_id, status, hold_reason)


async def review(conn, media_id, user: str, action: str, workspace: str = "ws_verified"):
    await conn.execute(
        "INSERT INTO media_reviews (media_id, workspace_id, user_id, action) VALUES ($1, $2, $3, $4)",
        media_id, workspace, user, action,
    )


async def test_new_media_must_start_pending(dbconn):
    with pytest.raises(asyncpg.RaiseError, match="must start pending"):
        await dbconn.execute(
            "INSERT INTO media (workspace_id, user_id, type, source, frames, status) "
            "VALUES ('ws_verified', 'member_v', 'image', 'upload', '[{}]', 'published')"
        )


async def test_publish_without_result_fails(dbconn):
    media_id = await new_media(dbconn)
    with pytest.raises(asyncpg.RaiseError, match="requires a complete published result"):
        await set_status(dbconn, media_id, "published")


async def test_publish_with_partial_coverage_fails(dbconn):
    media_id = await new_media(dbconn, n_frames=20)
    await add_result(dbconn, media_id, "published", n_frames=8)  # the incident: only 8 frames scored
    with pytest.raises(asyncpg.RaiseError, match="requires a complete published result"):
        await set_status(dbconn, media_id, "published")


async def test_held_result_needs_an_admin_approval_to_publish(dbconn):
    media_id = await new_media(dbconn)
    await add_result(dbconn, media_id, "held")
    await set_status(dbconn, media_id, "held", "needs_human_review")
    with pytest.raises(asyncpg.RaiseError):
        await set_status(dbconn, media_id, "published")  # no approval row
    await review(dbconn, media_id, "member_v", "approve")  # a member's approval does not count
    with pytest.raises(asyncpg.RaiseError):
        await set_status(dbconn, media_id, "published")
    await review(dbconn, media_id, "admin_v", "approve")
    await set_status(dbconn, media_id, "published")


async def test_cross_workspace_reviewer_rejected_by_foreign_key(dbconn):
    media_id = await new_media(dbconn)
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await review(dbconn, media_id, "admin_u", "approve", workspace="ws_unverified")


async def test_terminal_states_stay_terminal(dbconn):
    blocked = await new_media(dbconn)
    await add_result(dbconn, blocked, "blocked")
    await set_status(dbconn, blocked, "blocked")
    with pytest.raises(asyncpg.RaiseError, match="terminal"):
        await set_status(dbconn, blocked, "published")
    published = await new_media(dbconn)
    await add_result(dbconn, published, "published")
    await set_status(dbconn, published, "published")
    with pytest.raises(asyncpg.RaiseError, match="terminal"):
        await set_status(dbconn, published, "held", "needs_human_review")


async def test_holds_need_the_right_evidence(dbconn):
    media_id = await new_media(dbconn)
    with pytest.raises(asyncpg.RaiseError, match="human-review hold"):
        await set_status(dbconn, media_id, "held", "needs_human_review")
    await set_status(dbconn, media_id, "held", "provider_unavailable")  # fail-closed hold needs no result
    with pytest.raises(asyncpg.CheckViolationError):
        await set_status(dbconn, media_id, "held", None)  # held always carries a reason


async def test_results_and_reviews_are_append_only(dbconn):
    media_id = await new_media(dbconn)
    await add_result(dbconn, media_id, "published")
    with pytest.raises(asyncpg.RaiseError, match="append_only"):
        await dbconn.execute("UPDATE screening_results SET decision = 'blocked' WHERE media_id = $1", media_id)
    with pytest.raises(asyncpg.RaiseError, match="append_only"):
        await dbconn.execute("DELETE FROM screening_results WHERE media_id = $1", media_id)
    with pytest.raises(asyncpg.UniqueViolationError):
        await add_result(dbconn, media_id, "blocked")  # scored successfully at most once
    await review(dbconn, media_id, "admin_v", "reject")
    with pytest.raises(asyncpg.RaiseError, match="append_only"):
        await dbconn.execute("DELETE FROM media_reviews WHERE media_id = $1", media_id)


async def test_valid_automated_publish_and_block(dbconn):
    ok = await new_media(dbconn, n_frames=20)
    await add_result(dbconn, ok, "published", n_frames=20)
    await set_status(dbconn, ok, "published")
    bad = await new_media(dbconn)
    await add_result(dbconn, bad, "blocked")
    await set_status(dbconn, bad, "blocked")
    rejected = await new_media(dbconn)
    await set_status(dbconn, rejected, "held", "provider_unavailable")
    await review(dbconn, rejected, "admin_v", "reject")
    await set_status(dbconn, rejected, "blocked")  # admin rejection needs no result
