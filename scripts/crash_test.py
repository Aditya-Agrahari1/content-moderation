"""Worker crash after the provider scored a video but before the result was saved (real SIGKILL).

Runs on the host (stdlib + docker CLI) against the stack started with docker-compose.test.yml:
  python scripts/crash_test.py [--out evidence/after/crash_test.txt]

1. Provider in slow mode (8 s). The fake provider scores on receipt, so the work is done (and would
   be billed) while the worker is still waiting for the response.
2. Upload a 20-frame video; wait until /_stats shows scored == 1.
3. SIGKILL every worker container. Nothing can be saved: the process is gone.
4. Check the media is not published. Restart workers with the provider back to ok.
5. The lease expires, the sweeper re-queues the job, a worker re-claims it and calls /score again
   with the same Idempotency-Key; the provider answers from its stored result.
"""

import argparse
import json
import subprocess
import sys
import time
import urllib.request

API = "http://localhost:8000"
PROVIDER = "http://localhost:8001"
COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.test.yml"]
lines: list[str] = []


def say(message: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {message}"
    print(line, flush=True)
    lines.append(line)


def http(method: str, url: str, body: dict | None = None, headers: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def sql(query: str) -> str:
    out = subprocess.run([*COMPOSE, "exec", "-T", "db", "psql", "-U", "app", "-d", "moderation", "-tAc", query],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def stats(media_id: str) -> tuple[int, int]:
    s = http("GET", f"{PROVIDER}/_stats")
    return s["calls"].get(media_id, 0), s["scored"].get(media_id, 0)


def status(media_id: str) -> str:
    return http("GET", f"{API}/media/{media_id}", headers={"X-User-Id": "member_v", "X-Workspace-Id": "ws_verified"})[
        "status"]


def wait(predicate, timeout: float, what: str) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    say(f"FAIL: timed out waiting for {what}")
    raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out")
    args = parser.parse_args()
    failures = []

    http("POST", f"{PROVIDER}/_control", {"mode": "slow", "delay_ms": 8000})
    frames = [{"explicit": 0.02} for _ in range(20)]
    frames[19] = {"explicit": 0.9}  # explicit is fine in a verified workspace -> expect published
    media_id = http("POST", f"{API}/media", {"type": "video", "source": "upload", "frames": frames},
                    {"X-User-Id": "member_v", "X-Workspace-Id": "ws_verified"})["id"]
    say(f"uploaded 20-frame video {media_id}; provider in slow mode (8 s)")

    wait(lambda: stats(media_id)[1] == 1, 20, "the provider to score the video")
    calls, scored = stats(media_id)
    say(f"provider has scored it (calls={calls}, scored={scored}); worker is waiting for the response")

    subprocess.run([*COMPOSE, "kill", "-s", "SIGKILL", "worker"], check=True, capture_output=True)
    say("SIGKILL sent to every worker container")
    killed_at = time.time()
    st, results = status(media_id), sql(f"SELECT count(*) FROM screening_results WHERE media_id = '{media_id}'")
    say(f"after the crash: status={st}, screening_results rows={results}")
    if st == "published" or results != "0":
        failures.append("media published or result saved by a dead worker")
    say("job row: " + sql(f"SELECT status, attempt, lease_expires > now() FROM screening_jobs WHERE media_id = '{media_id}'"))

    http("POST", f"{PROVIDER}/_control", {"mode": "ok"})
    subprocess.run([*COMPOSE, "up", "-d", "worker"], check=True, capture_output=True)
    say("provider back to ok; workers restarted")

    wait(lambda: status(media_id) == "published", 60, "recovery")
    say(f"recovered: status=published {time.time() - killed_at:.1f}s after the crash")
    calls, scored = stats(media_id)
    results = sql(f"SELECT count(*) FROM screening_results WHERE media_id = '{media_id}'")
    attempts = sql("SELECT string_agg(attempt_number || ':' || COALESCE(outcome, 'never finished'), ', ' "
                   "ORDER BY attempt_number) FROM job_attempts a JOIN screening_jobs j ON j.id = a.job_id "
                   f"WHERE j.media_id = '{media_id}'")
    say(f"provider calls={calls}, provider scorings={scored}, screening_results rows={results}")
    say(f"job_attempts: {attempts}")
    if scored != 1:
        failures.append(f"scored {scored} times")
    if results != "1":
        failures.append(f"{results} result rows")

    say("PASS" if not failures else "FAIL: " + "; ".join(failures))
    if args.out:
        with open(args.out, "w") as fh:
            fh.write("\n".join(lines) + "\n")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
