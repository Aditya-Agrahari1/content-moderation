"""Guided live demo. Runs each step, prints what happened, and waits for Enter between steps.

    python scripts/demo.py            (from the project folder; PowerShell or Git Bash)

Needs the stack running:
    docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
and the review page open at http://localhost:3000.
"""

import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

API = "http://localhost:8000"
PROVIDER = "http://localhost:8001"
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.test.yml"]
WHO = {
    "member_v": "ws_verified", "admin_v": "ws_verified",
    "member_u": "ws_unverified", "admin_u": "ws_unverified",
}
BOLD, GREEN, RED, YELLOW, DIM, END = "\033[1m", "\033[92m", "\033[91m", "\033[93m", "\033[2m", "\033[0m"


def call(method: str, url: str, body=None, user: str | None = None, raw: bytes | None = None, headers=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    h = {"Content-Type": "application/json", **(headers or {})}
    if user:
        h.update({"X-User-Id": user, "X-Workspace-Id": WHO[user]})
    request = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read() or b"null")
    except (urllib.error.URLError, ConnectionError, TimeoutError):  # API restarting / down
        return 0, None


def title(text: str) -> None:
    print(f"\n{BOLD}{'=' * 78}\n{text}\n{'=' * 78}{END}")


def say(text: str) -> None:
    print(f"{YELLOW}  Say: {text}{END}")


def show(label: str, status: int, body) -> None:
    color = GREEN if status < 300 else RED
    print(f"  {label:<46} {color}HTTP {status}{END}  {json.dumps(body)}")


def pause(text: str = "Press Enter for the next step...") -> None:
    input(f"\n{DIM}  {text}{END}")


def upload(user: str, body: dict) -> str:
    status, response = call("POST", f"{API}/media", body, user)
    show(f"POST /media as {user}", status, response)
    return response["id"]


def wait_status(user: str, media_id: str, wanted: tuple, timeout: float = 40) -> dict:
    started = time.time()
    while time.time() - started < timeout:
        _, media = call("GET", f"{API}/media/{media_id}", user=user)
        if media["status"] in wanted:
            break
        time.sleep(0.5)
    print(f"  after {time.time() - started:4.1f}s  GET /media/{media_id[:8]}...  ->  {BOLD}{media}{END}")
    return media


def trace(media_id: str) -> None:
    """The structured logs of one media item, across api, worker and provider."""
    out = subprocess.run([*COMPOSE, "logs", "--no-log-prefix", "api", "worker", "provider"],
                         capture_output=True, text=True, encoding="utf-8", errors="replace").stdout
    rows = []
    for line in out.splitlines():
        if media_id in line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                pass
    print(f"  {DIM}logs for {media_id} (docker compose logs | grep <id>):{END}")
    for r in sorted(rows, key=lambda r: r.get("ts", "")):
        extra = " ".join(f"{k}={r[k]}" for k in ("attempt", "cycle", "provider_outcome", "to_status", "retry_in_s",
                                                   "mode", "decision") if r.get(k) not in (None, ""))
        print(f"    {r.get('ts', '')[11:23]}  {r.get('service', ''):<8} {r['event']:<18} {extra}")


def main() -> None:
    import os

    os.system("")  # turns on colour codes in the Windows console
    status, _ = call("GET", f"{API}/healthz")
    if status != 200:
        sys.exit("API not running. Start: docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build")
    call("POST", f"{PROVIDER}/_control", {"mode": "ok"})
    ids = []

    title("STEP 1  Bug 2: every frame counts (explicit content in the LAST frame, unverified workspace)")
    media_id = upload("member_u", {"type": "video", "source": "upload",
                                   "frames": [{"explicit": 0.02}] * 19 + [{"explicit": 0.95}]})
    ids.append(media_id)
    wait_status("member_u", media_id, ("published", "blocked"))
    say("The old handler screened only the first 8 frames, so this video was published. Now it is blocked.")
    pause()

    title("STEP 2  Bug 3: prohibited generation prompts are refused before anything is generated")
    for prompt in ["explicit photo of a 16-year-old girl", "nude s1xteen year old", "two adults, both 25, on a beach"]:
        status, body = call("POST", f"{API}/media", {"type": "image", "source": "generation", "prompt": prompt,
                                                      "frames": [{}]}, "member_v")
        show(f"prompt: {prompt!r}"[:46], status, body)
        if status == 202:
            ids.append(body["id"])
    say("Refused with 422 and a reason, even with leetspeak. A normal adult prompt is accepted.")
    pause()

    title("STEP 3  Bugs 1 + 5: provider outage -> media is HELD (never published) -> automatic recovery")
    status, body = call("POST", f"{PROVIDER}/_control", {"mode": "error"})
    show("provider POST /_control {mode: error}", status, body)
    media_id = upload("member_u", {"type": "image", "source": "upload", "frames": [{"explicit": 0.01}]})
    ids.append(media_id)
    wait_status("member_u", media_id, ("held",))
    _, queue = call("GET", f"{API}/review", user="admin_u")
    item = next(i for i in queue if i["id"] == media_id)
    print(f"  GET /review as admin_u  ->  hold_reason={item['hold_reason']}  reviewable={item['reviewable']}")
    status, body = call("POST", f"{API}/review/{media_id}/approve", user="admin_u")
    show("admin_u tries to APPROVE it", status, body)
    say("Old handler: a timeout counted as a pass and this went live. Now it is held, visible to moderators,")
    say("and nobody can approve something that was never screened.")
    print(f"\n  {BOLD}>> In the browser: choose 'admin_u (ws_unverified)'. The item shows 'Waiting for re-screen'"
          f" and Approve is greyed out.{END}")
    pause("Press Enter to bring the provider back...")
    status, body = call("POST", f"{PROVIDER}/_control", {"mode": "ok"})
    show("provider POST /_control {mode: ok}", status, body)
    print("  waiting for the worker to notice the provider is back (circuit breaker cooldown)...")
    wait_status("member_u", media_id, ("published",), timeout=60)
    trace(media_id)
    say("No human did anything: retries, circuit breaker, then the sweeper re-screened it and it was published.")
    print(f"  {BOLD}>> Refresh the browser: the item is gone from admin_u's queue.{END}")
    pause()

    title("STEP 4  Access rules: other workspaces, members, unsigned callbacks")
    held_id = upload("member_v", {"type": "image", "source": "upload", "frames": [{"self_harm": 0.6}]})
    ids.append(held_id)
    wait_status("member_v", held_id, ("held",))
    show("admin_u (other workspace) reads it", *call("GET", f"{API}/media/{held_id}", user="admin_u"))
    show("member_v tries to approve", *call("POST", f"{API}/review/{held_id}/approve", user="member_v"))
    show("callback without a signature", *call("POST", f"{API}/provider/callback", raw=b'{"media_id":"x"}'))
    say("Other workspaces get 404, members get 403, unsigned provider callbacks get 401.")
    print(f"\n  {BOLD}>> In the browser: choose 'admin_v (ws_verified)', find the self_harm_review item, click Approve."
          f"{END}")
    print(f"  {BOLD}>> Then choose 'member_v': the page shows an ERROR with Retry (403), not 'Nothing to review'.{END}")
    pause()

    title("STEP 5  Bug 5: a failed review request is shown as a failure, never as an empty queue")
    waiting = upload("member_v", {"type": "image", "source": "upload", "frames": [{"minor": 0.6}]})
    ids.append(waiting)
    wait_status("member_v", waiting, ("held",))
    print(f"  {BOLD}>> In the browser (admin_v): click Refresh. The queue shows this item ({waiting[:8]}..., minor_review)."
          f"{END}")
    pause("Press Enter to STOP the API...")
    subprocess.run([*COMPOSE, "stop", "api"], capture_output=True)
    print(f"  {RED}API stopped.{END}")
    print(f"  {BOLD}>> In the browser (admin_v): within 10 s the page shows 'Could not load the review queue: the API is"
          f" unreachable' and a Retry button by itself (or click Refresh to see it at once).{END}")
    say("In the incident this looked like 'Nothing to review' while 40 items waited.")
    say("Now a failure is always shown as a failure, and the page keeps re-checking every 10 seconds.")
    pause("Press Enter to start the API again...")
    subprocess.run([*COMPOSE, "start", "api"], capture_output=True)
    for _ in range(60):
        if call("GET", f"{API}/healthz")[0] == 200:
            break
        time.sleep(1)
    print(f"  {GREEN}API back.{END}  >> In the browser click Retry (or wait 10 s): the item {waiting[:8]}... is back"
          f" in the queue. Nothing was lost; the page just could not reach the API.")
    pause()

    title("STEP 6  Bug 4 and cost: each media item is scored once")
    _, stats = call("GET", f"{PROVIDER}/_stats")
    for media_id in ids:
        print(f"  {media_id}  calls={stats['calls'].get(media_id, 0)}  scored={stats['scored'].get(media_id, 0)}")
    say("'calls' counts every request, including failed ones during the outage. 'scored' is the paid work:")
    say("never more than 1. Optional next: python scripts/crash_test.py (real kill -9 of the workers).")
    print(f"\n{GREEN}{BOLD}Demo finished.{END}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        call("POST", f"{PROVIDER}/_control", {"mode": "ok"})
        print("\nstopped (provider set back to ok)")
