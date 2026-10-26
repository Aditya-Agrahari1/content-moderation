"""Check every rule against the running stack and print: what we did, expected, actual, proof, PASS/FAIL.

    python scripts/check_all.py

Needs: docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
"""

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

API, PROVIDER = "http://localhost:8000", "http://localhost:8001"
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.test.yml"]
WS = {"member_v": "ws_verified", "admin_v": "ws_verified", "member_u": "ws_unverified", "admin_u": "ws_unverified"}
SECRET = os.getenv("PROVIDER_CALLBACK_SECRET", "local-dev-placeholder-not-secret")
GREEN, RED, BOLD, DIM, END = "\033[92m", "\033[91m", "\033[1m", "\033[2m", "\033[0m"
results: list[bool] = []
checked_ids: list[str] = []


def call(method, url, body=None, user=None, raw=None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", **(headers or {})}
    if user:
        h.update({"X-User-Id": user, "X-Workspace-Id": WS[user]})
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=h), timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")


def sql(query: str) -> str:
    out = subprocess.run([*COMPOSE, "exec", "-T", "db", "psql", "-U", "app", "-d", "moderation", "-tAc", query],
                         capture_output=True, text=True)
    return out.stdout.strip()


def mode(m: str, delay_ms: int | None = None) -> None:
    call("POST", f"{PROVIDER}/_control", {"mode": m} if delay_ms is None else {"mode": m, "delay_ms": delay_ms})


def upload(user: str, frames: list, kind: str | None = None, **extra) -> tuple[int, dict]:
    body = {"type": kind or ("image" if len(frames) == 1 else "video"), "source": "upload", "frames": frames, **extra}
    status, response = call("POST", f"{API}/media", body, user)
    if status == 202:
        checked_ids.append(response["id"])
    return status, response


def status_of(user: str, media_id: str, until=("published", "blocked", "held"), timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while True:
        _, media = call("GET", f"{API}/media/{media_id}", user=user)
        if media["status"] in until or time.time() > deadline:
            return media
        time.sleep(0.3)


def proof(media_id: str) -> str:
    _, stats = call("GET", f"{PROVIDER}/_stats")
    stored = sql(f"SELECT decision || ', frames_scored=' || frames_scored FROM screening_results WHERE media_id = '{media_id}'")
    return f"provider calls={stats['calls'].get(media_id, 0)} scored={stats['scored'].get(media_id, 0)}; db result: {stored or 'none'}"


def check(number: str, rule: str, did: str, expected: str, actual: str, ok: bool, evidence: str = "") -> None:
    results.append(ok)
    mark = f"{GREEN}PASS{END}" if ok else f"{RED}FAIL{END}"
    print(f"\n{BOLD}[{number}] {rule}{END}  {mark}")
    print(f"     did:      {did}")
    print(f"     expected: {expected}")
    print(f"     actual:   {actual}")
    if evidence:
        print(f"     {DIM}proof:    {evidence}{END}")


def media_check(number, rule, user, frames, expected_status, did, expected_reason=None):
    _, created = upload(user, frames)
    media = status_of(user, created["id"])
    ok = media["status"] == expected_status and (expected_reason is None or expected_reason in media["reasons"])
    check(number, rule, did, f"{expected_status}" + (f" ({expected_reason})" if expected_reason else ""),
          f"{media['status']} {media['reasons']}", ok, proof(created["id"]))
    return created["id"]


def main() -> None:
    os.system("")  # colours in the Windows console
    if call("GET", f"{API}/healthz")[0] != 200:
        sys.exit("API not running. Start: docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build")
    mode("ok")
    print(f"{BOLD}Checking every rule against the running system...{END}")

    print(f"\n{BOLD}--- Rules 2 and 3: the moderation policy ---{END}")
    media_check("1", "Clean image is published", "member_v", [{"explicit": 0.1}], "published",
                "image with explicit=0.1 in ws_verified")
    media_check("2", "Explicit is allowed in a VERIFIED workspace", "member_v", [{"explicit": 0.95}], "published",
                "image with explicit=0.95 in ws_verified")
    media_check("3", "Explicit is blocked in an UNVERIFIED workspace", "member_u", [{"explicit": 0.95}], "blocked",
                "image with explicit=0.95 in ws_unverified", "explicit_unverified")
    for i, label in enumerate(["minor", "sexual_violence", "bestiality", "self_harm", "real_person"]):
        media_check(f"4.{i + 1}", f"Prohibited '{label}' >= 0.8 blocks even in a verified workspace", "member_v",
                    [{label: 0.8}], "blocked", f"image with {label}=0.8 in ws_verified", f"{label}_block")
    media_check("5a", "Score 0.4999 is below the review band", "member_v", [{"self_harm": 0.4999}], "published",
                "image with self_harm=0.4999")
    held = media_check("5b", "Score 0.5-0.8 is held for a human", "member_v", [{"self_harm": 0.5}], "held",
                       "image with self_harm=0.5", "self_harm_review")

    print(f"\n{BOLD}--- Rule 9 (bug 2): every frame of a video counts ---{END}")
    media_check("6", "Prohibited content in a LATE frame is caught", "member_v",
                [{"explicit": 0.01}] * 15 + [{"minor": 0.95}] + [{"explicit": 0.01}] * 4, "blocked",
                "20-frame video, minor=0.95 only at frame 16 (old code checked 8 frames)", "minor_block")
    long_id = media_check("7", "Long video (40 frames, async callback): last frame counts", "member_u",
                          [{"explicit": 0.01}] * 39 + [{"explicit": 0.95}], "blocked",
                          "40-frame video, explicit=0.95 only in the last frame, ws_unverified", "explicit_unverified")

    print(f"\n{BOLD}--- Rule 4 (bug 3): prohibited generation prompts are refused ---{END}")
    for n, (prompt, want) in enumerate([("explicit photo of a 16-year-old girl", 422), ("nude s1xteen year old", 422),
                                        ("brother and sister having sex", 422),
                                        ("two adults, both 25, on a beach", 202)]):
        st, body = call("POST", f"{API}/media", {"type": "image", "source": "generation", "prompt": prompt,
                                                  "frames": [{}]}, "member_v")
        check(f"8.{n + 1}", "Prompt " + ("refused" if want == 422 else "allowed"), f"generation prompt {prompt!r}",
              f"HTTP {want}", f"HTTP {st} {body}", st == want)

    print(f"\n{BOLD}--- Rules 1 and 6: workspace isolation and admin-only review ---{END}")
    st, _ = call("GET", f"{API}/media/{held}", user="admin_u")
    check("9", "Another workspace cannot see the media", "admin_u (ws_unverified) reads a ws_verified item",
          "HTTP 404", f"HTTP {st}", st == 404)
    st, _ = call("GET", f"{API}/media/{held}", headers={"X-User-Id": "member_v", "X-Workspace-Id": "ws_unverified"})
    check("9b", "Unknown or mismatched identity cannot see it either",
          "member_v claiming to be in ws_unverified reads the item", "HTTP 404", f"HTTP {st}", st == 404)
    st, _ = call("GET", f"{API}/media/{held}")
    check("10", "No identity headers", "GET /media/{id} without X-User-Id / X-Workspace-Id", "HTTP 401",
          f"HTTP {st}", st == 401)
    st, body = call("POST", f"{API}/review/{held}/approve", user="member_v")
    check("11", "A member cannot approve", "member_v approves a held item", "HTTP 403", f"HTTP {st} {body}", st == 403)
    _, queue = call("GET", f"{API}/review", user="admin_v")
    item = next((i for i in queue if i["id"] == held), None)
    check("12", "Held item appears in the admin's review queue", "GET /review as admin_v",
          "item listed, hold_reason=needs_human_review, reviewable=true",
          f"{item and (item['hold_reason'], item['reviewable'])}", bool(item and item["reviewable"]))
    st, body = call("POST", f"{API}/review/{held}/approve", user="admin_v")
    reviews = sql(f"SELECT user_id || ' ' || action FROM media_reviews WHERE media_id = '{held}'")
    check("13", "An admin can approve", "admin_v approves it", "HTTP 200, published", f"HTTP {st} {body}",
          st == 200 and body["status"] == "published", f"db media_reviews: {reviews}")

    print(f"\n{BOLD}--- Rule 7: provider callbacks must be signed ---{END}")
    raw = json.dumps({"media_id": long_id, "request_id": "fake", "frames": [{}]}).encode()
    st, _ = call("POST", f"{API}/provider/callback", raw=raw)
    check("14", "Unsigned callback rejected", "POST /provider/callback with no X-Signature", "HTTP 401", f"HTTP {st}",
          st == 401)
    bad = hmac.new(b"wrong-secret", raw, hashlib.sha256).hexdigest()
    st, _ = call("POST", f"{API}/provider/callback", raw=raw, headers={"X-Signature": bad})
    check("15", "Callback signed with the wrong secret rejected", "X-Signature computed with a wrong secret",
          "HTTP 401", f"HTTP {st}", st == 401)

    print(f"\n{BOLD}--- Rule 5 (bug 1): no decision -> held, never published; automatic recovery ---{END}")
    mode("timeout")
    _, created = upload("member_u", [{"explicit": 0.01}])
    media = status_of("member_u", created["id"], until=("held", "published"))
    check("16", "Provider TIMEOUT -> held, not published", "provider set to timeout, then upload a clean image",
          "held (provider_unavailable)", f"{media['status']} {media['reasons']}", media["status"] == "held",
          proof(created["id"]))
    st, body = call("POST", f"{API}/review/{created['id']}/approve", user="admin_u")
    check("17", "Nobody can approve something never screened", "admin_u approves the outage-held item",
          "HTTP 409 not_screened", f"HTTP {st} {body}", st == 409)
    mode("ok")
    started = time.time()
    media = status_of("member_u", created["id"], until=("published",), timeout=60)
    check("18", "Provider back -> re-screened automatically, no human",
          "provider set back to ok, then wait", "published",
          f"{media['status']} after {time.time() - started:.1f}s", media["status"] == "published", proof(created["id"]))

    print(f"\n{BOLD}--- Rule 8 (bug 4): scored at most once, even if a job is redelivered ---{END}")
    mode("slow", 1500)
    _, created = upload("member_v", [{"explicit": 0.01}])
    deadline = time.time() + 10
    while call("GET", f"{PROVIDER}/_stats")[1]["calls"].get(created["id"], 0) < 1 and time.time() < deadline:
        time.sleep(0.1)
    sql(f"UPDATE screening_jobs SET status = 'queued' WHERE media_id = '{created['id']}'")
    status_of("member_v", created["id"], until=("published",))
    time.sleep(2)
    _, stats = call("GET", f"{PROVIDER}/_stats")
    calls = stats["calls"].get(created["id"], 0)
    check("19", "Redelivered job does not call the provider again",
          "provider slow; while the call is running, put the SAME job back to 'queued' in the database",
          "provider calls = 1", f"provider calls = {calls}", calls == 1, proof(created["id"]))
    mode("ok")

    _, stats = call("GET", f"{PROVIDER}/_stats")
    worst = max(stats["scored"].get(i, 0) for i in checked_ids)
    check("20", "Every media item in this run scored at most once", f"/_stats for all {len(checked_ids)} items",
          "max scored = 1", f"max scored = {worst}", worst <= 1)

    print(f"\n{BOLD}--- Proof from the database ---{END}")
    unchecked = sql("SELECT count(*) FROM media m LEFT JOIN screening_results r ON r.media_id = m.id "
                    "AND r.frames_scored = m.frame_count WHERE m.status = 'published' AND (r.media_id IS NULL OR NOT "
                    "(r.decision = 'published' OR (r.decision = 'held' AND EXISTS (SELECT 1 FROM media_reviews v "
                    "WHERE v.media_id = m.id AND v.action = 'approve'))))")
    check("21", "Nothing published without a complete screening", "scan every published row in the database",
          "0 rows", f"{unchecked} rows", unchecked == "0")
    out = subprocess.run([*COMPOSE, "exec", "-T", "db", "psql", "-U", "app", "-d", "moderation", "-c",
                          f"BEGIN; UPDATE media SET status = 'published' WHERE id = '{long_id}'; ROLLBACK;"],
                         capture_output=True, text=True)
    refused = "media_guard" in out.stderr or "terminal" in out.stderr
    check("22", "Database itself refuses to publish blocked/unscreened media",
          "UPDATE media SET status='published' directly in SQL (bypassing the app)", "ERROR from the trigger",
          (out.stderr.strip().splitlines() or ["no error"])[0], refused)

    passed = sum(results)
    color = GREEN if passed == len(results) else RED
    print(f"\n{BOLD}{color}{passed} / {len(results)} checks passed{END}")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    try:
        main()
    finally:
        mode("ok")
