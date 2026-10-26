# PLAN.md — Content Moderation Service (Revision 2)

> **Status:** Design plan written and reviewed before implementation; kept as the record of what was planned. Where the build differs, README §8 lists the deviations.
>
> **Scope rule:** This document is the implementation contract. If implementation reveals a conflict, stop, update this plan and its tests, then continue. Do not code around the plan.
>
> **Time rule:** The assignment allows 6–7 hours including setup. Section 22 gives the time budget and the order in which work is cut if the budget is at risk. Actual time spent is recorded in the README.

### Changes from Revision 1

- **Recovery:** fixed the deadlock that could leave the circuit open forever. The sweeper now re-queues one existing `held_provider` job as the probe call.
- **Attempt budget:** recovery cycles now reset the per-cycle attempt budget. A skip while the circuit is open costs nothing. 429 responses have their own, separate limit.
- **Backlog metric:** "oldest unresolved job" now covers every unresolved job state. Failed jobs have their own metric and alert.
- **Media access:** "owner" now means any member of the owning workspace.
- **Redelivery test:** the harness resets the same job row directly in the database, and the same test runs at both tags.
- **Provider modes:** exactly `ok`, `slow`, `timeout` and `error`, with `fail` accepted as an alias.
- **Provider contract:** `POST /score` request, response and callback schemas are now defined. The provider retries callback delivery.
- **Removed:**
  - the SQL policy function and the decision recomputation in the database
  - callback field aliases
  - `workspace_verified_at_decision`
  - one Redis wake-up list per workspace (now one list)
  - the media source-field immutability trigger
- **Unknown labels:** rejected with `422`.
- **Workload:** burst videos have 20 frames (synchronous path). A 40-frame variant exercises the asynchronous path.
- **Auth:** 401 and 403 are decided before any media lookup, so they leak nothing. 404 now covers only "valid identity, media not in your workspace or not found".
- **Docs:** consolidated so the work fits the time budget (Section 20).

---

## 1. Architecture

```
docker compose up
  frontend (React+TS, :3000) ──HTTP──▶ api (FastAPI, :8000) ──▶ PostgreSQL 16 (source of truth)
                                         │  ▲                     ▲
                                         │  │ signed callback     │
                                         ▼  │                     │
                     Redis 7 ◀──────── worker ×2 (4 async tasks each) ──HTTP──▶ provider (FastAPI, :8001)
           (wake-up list, circuit breaker,                                         │
            429 pause, best-effort counters)            provider ──POST /provider/callback──▶ api
```

| Container | Image | Purpose |
|---|---|---|
| `db` | postgres:16 | Media, jobs, attempts, results, reviews |
| `redis` | redis:7 | Wake-ups, circuit-breaker state, 429 pause, counters. **Not durable state.** |
| `provider` | Python 3.12 + FastAPI | Fake moderation provider on :8001 |
| `api` | Python 3.12 + FastAPI | Public API and callback endpoint. Runs `alembic upgrade head` and the idempotent seed on start. |
| `worker` | Same image as `api`, `python -m app.worker` | `deploy.replicas: 2`, `WORKER_CONCURRENCY=4` tasks each. Also runs the sweeper (one leader, chosen by PostgreSQL advisory lock). |
| `frontend` | Node 20, Vite build + `vite preview` | One review-queue page on :3000 |

Every service has a healthcheck, `depends_on` with `condition: service_healthy`, and runs as a non-root user.

### Why a PostgreSQL-backed queue

- **Atomicity:** the media row and its job row are created in one transaction. Nothing can be lost between committing to the database and publishing to a broker, so no outbox is needed.
- **Crash recovery:** a crashed worker's lease simply expires, and the same job row becomes claimable again.
- **Explainability:** fairness, the per-workspace cap and fencing are all in one SQL claim transaction, which can be tested against real PostgreSQL.
- **Cost:** it adds no infrastructure beyond what the assignment already requires.

Trade-off: claim-query contention limits throughput at high scale (Part 3, 1M/day answer). This is recorded in DECISIONS.md.

### What Redis does

Redis is used for shared, short-lived coordination. Losing it loses no work.

| Key | Use | If Redis is down |
|---|---|---|
| `jobs:wakeup` (list) | API `LPUSH`es after commit; workers `BRPOP` with a timeout of `WORKER_POLL_INTERVAL_S` | Workers poll PostgreSQL every `WORKER_POLL_INTERVAL_S` |
| `cb:*` | Circuit-breaker state shared by all workers (Section 7) | Each worker process keeps its own in-memory breaker (more probe calls, still safe) |
| `cb:canary` | `SET NX EX`: exactly one half-open probe call at a time | In-memory, one canary per process |
| `provider:pause_until` | Global 429 `Retry-After` pause | In-memory, per process |
| `metrics:*` | Best-effort counters (callback rejections, duplicates) | Counters are lost; logs still have the events |

PostgreSQL alone decides fairness, the workspace cap, ownership and results.

---

## 2. Data Model

### 2.1 Baseline schema (migration `0001_naive`, present at `broken-baseline`)

```sql
CREATE TABLE workspaces (id TEXT PRIMARY KEY, verified BOOLEAN NOT NULL);
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    role TEXT NOT NULL CHECK (role IN ('admin','member')),
    UNIQUE (id, workspace_id)
);
CREATE TABLE media (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id TEXT NOT NULL REFERENCES workspaces(id),
    user_id TEXT NOT NULL,
    type TEXT NOT NULL, source TEXT NOT NULL, prompt TEXT,
    frames JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    reasons JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE screening_jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    media_id UUID NOT NULL REFERENCES media(id),
    workspace_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',      -- baseline uses queued / processing / done / failed
    attempt INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**Columns shared by both tags.** Black-box tests, the workload script and audit checks A and B may use only these: `media.id`, `media.workspace_id`, `media.frames`, `media.status`, `media.created_at`, `screening_jobs.media_id`, `screening_jobs.status`, `screening_jobs.created_at`, plus the HTTP API.

### 2.2 Fixed schema (migration `0002_hardening`, added after the tag)

```sql
-- media
ALTER TABLE media
  ADD COLUMN hold_reason TEXT
      CHECK (hold_reason IN ('needs_human_review','provider_unavailable','screening_failed')),
  ADD COLUMN frame_count INT GENERATED ALWAYS AS (jsonb_array_length(frames)) STORED,
  ADD CONSTRAINT chk_status CHECK (status IN ('pending','published','held','blocked')),
  ADD CONSTRAINT chk_type   CHECK (type IN ('image','video')),
  ADD CONSTRAINT chk_source CHECK (source IN ('upload','generation')),
  ADD CONSTRAINT chk_frames_array CHECK (jsonb_typeof(frames)='array' AND jsonb_array_length(frames) >= 1),
  ADD CONSTRAINT chk_image_one_frame CHECK (type <> 'image' OR jsonb_array_length(frames) = 1),
  ADD CONSTRAINT chk_generation_prompt CHECK (source <> 'generation' OR prompt IS NOT NULL),
  ADD CONSTRAINT chk_held_reason CHECK ((status = 'held') = (hold_reason IS NOT NULL)),
  ADD CONSTRAINT fk_media_user_ws FOREIGN KEY (user_id, workspace_id) REFERENCES users(id, workspace_id),
  ADD CONSTRAINT uq_media_id_ws UNIQUE (id, workspace_id);

-- screening_jobs: one row per media, reused for every retry, redelivery and recovery cycle
ALTER TABLE screening_jobs
  ADD COLUMN cycle INT NOT NULL DEFAULT 1,               -- recovery cycle number
  ADD COLUMN cycle_failures INT NOT NULL DEFAULT 0,      -- retryable failures this cycle
  ADD COLUMN cycle_rate_limited INT NOT NULL DEFAULT 0,  -- 429 / 409 responses this cycle
  ADD COLUMN malformed_total INT NOT NULL DEFAULT 0,     -- lifetime malformed responses
  ADD COLUMN next_retry_at TIMESTAMPTZ,
  ADD COLUMN lease_token UUID,                           -- fencing token; ownership = token + live lease
  ADD COLUMN lease_expires TIMESTAMPTZ,                  -- NULL when nobody owns the job
  ADD COLUMN callback_deadline TIMESTAMPTZ,
  ADD COLUMN last_error_class TEXT,
  ADD CONSTRAINT chk_job_status CHECK (status IN
      ('queued','claimed','waiting_callback','held_provider','completed','failed','cancelled')),
  ADD CONSTRAINT fk_job_media_ws FOREIGN KEY (media_id, workspace_id) REFERENCES media(id, workspace_id);
-- `attempt` keeps its baseline meaning: lifetime count of provider HTTP calls made for this job.

CREATE UNIQUE INDEX uq_one_unresolved_job_per_media ON screening_jobs(media_id)
  WHERE status IN ('queued','claimed','waiting_callback','held_provider');
CREATE INDEX ix_jobs_claim ON screening_jobs(status, next_retry_at, workspace_id, created_at);

CREATE TABLE job_attempts (                 -- one row per provider HTTP call
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id UUID NOT NULL REFERENCES screening_jobs(id),
    attempt_number INT NOT NULL,
    cycle INT NOT NULL,
    lease_token UUID NOT NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    outcome TEXT,          -- success | async_accepted | timeout | conn_error | server_error
                           -- | rate_limited | in_progress | malformed | client_error | stale_discarded
    http_status INT,
    latency_ms INT,
    UNIQUE (job_id, attempt_number)
);

CREATE TABLE screening_results (           -- at most one per media: "scored successfully at most once"
    media_id UUID PRIMARY KEY REFERENCES media(id),
    job_id UUID NOT NULL REFERENCES screening_jobs(id),
    provider_request_id TEXT,
    raw_response JSONB NOT NULL,           -- canonical {"frames":[{six labels}, ...]}
    frames_scored INT GENERATED ALWAYS AS (jsonb_array_length(raw_response->'frames')) STORED,
    decision TEXT NOT NULL CHECK (decision IN ('published','held','blocked')),
    reasons JSONB NOT NULL,                -- detailed: [{label, score, frame_index, rule}]
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT chk_result_frames CHECK (jsonb_typeof(raw_response->'frames') = 'array')
);

CREATE TABLE media_reviews (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    media_id UUID NOT NULL, workspace_id TEXT NOT NULL, user_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('approve','reject')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (media_id, workspace_id) REFERENCES media(id, workspace_id),
    FOREIGN KEY (user_id, workspace_id)  REFERENCES users(id, workspace_id)
);
```

Data in the baseline tables survives the migration. Existing `processing`/`done` job rows are mapped to `claimed`/`completed` by the migration before the CHECK constraint is added.

---

## 3. Database Publication Guard (fixed version only)

There are two triggers, installed by `0002_hardening` and written in plain PL/pgSQL. The database **does not** contain the policy.

**`media_guard`**, `BEFORE INSERT OR UPDATE OF status, hold_reason ON media`:

1. INSERT requires `status='pending'` and `hold_reason IS NULL`.
2. `published` and `blocked` are terminal: no transition out of them.
3. Nothing transitions back to `pending`.
4. `→ published` requires **either**:
   - (a) a `screening_results` row with `frames_scored = NEW.frame_count` and `decision = 'published'`, **or**
   - (b) a row with full coverage and `decision = 'held'`, **plus** a `media_reviews` row with `action='approve'` whose user has `role='admin'` in `NEW.workspace_id`.
5. `→ blocked` requires either a full-coverage result with `decision='blocked'`, or a `reject` review row by an admin of the workspace.
6. `→ held` with `hold_reason='needs_human_review'` requires a full-coverage result with `decision='held'`.
7. `→ held` with `provider_unavailable` or `screening_failed` needs no result. These are fail-closed availability states.

**`append_only`**, `BEFORE UPDATE OR DELETE ON screening_results, media_reviews`, always raises an error.

### Trust boundary (stated in the README)

The database guarantees:
- nothing is published without a stored result that covers every frame and records a `published` decision, or an admin approval of a `held` result;
- terminal states stay terminal;
- evidence rows are append-only.

The database does **not** check that the scores justify the recorded decision. That is the job of:
- the Python policy (`app/policy.py`), tested with table tests and a Hypothesis property test against an independent oracle (`tests/helpers/policy_oracle.py`);
- audit check B, which recomputes every published item's expected decision from its original input frames using that oracle.

The database also cannot prove that the scores came from the provider. That relies on response validation and the callback HMAC. It also cannot prove which human sent a request through the shared application credential. That relies on API identity, which is a test-harness mechanism here. A database owner or superuser can bypass the triggers; that is out of scope.

---

## 4. State Machines

### 4.1 Media status (the four statuses required by the contract)

| From | To | Trigger | Guard |
|---|---|---|---|
| — | `pending` | `POST /media` accepted | Media and job inserted in one transaction |
| `pending` | `held(provider_unavailable)` | First retryable failure, or worker sees the circuit open | None (fail-closed) |
| `pending` / `held(provider_unavailable \| screening_failed)` | `published` / `held(needs_human_review)` / `blocked` | A result is stored (sync response, retry or callback) | `media_guard` rules 4–6 |
| `pending` | `held(screening_failed)` | Non-retryable provider error (Section 6) | None (fail-closed) |
| `held(needs_human_review)` | `published` | Admin approves | Approve row + held result |
| `held(any reason)` | `blocked` | Admin rejects | Reject row; an unresolved job becomes `cancelled` |

`GET /review` returns every `held` item with `hold_reason` and `reviewable = (hold_reason == 'needs_human_review')`. Outage-held items are visible, so "Nothing to review" can never hide waiting work. They cannot be approved (`409`), but an admin may reject them.

### 4.2 Job status

| Status | Unresolved? | Meaning |
|---|---|---|
| `queued` | yes | Eligible once `next_retry_at` has passed **and** there is no live lease |
| `claimed` | yes | A worker task holds `lease_token` with `lease_expires > now()` |
| `waiting_callback` | yes | Provider returned `202`; `callback_deadline` is set; no lease |
| `held_provider` | yes | Per-cycle budget exhausted; waiting for provider recovery (Section 7) |
| `completed` | no | A result exists for the media |
| `failed` | no (operator) | Non-retryable error. Media is `held(screening_failed)`. Shown in metrics and raises an alert (Section 10). Never requeued automatically. |
| `cancelled` | no | Admin rejected the media before screening finished |

| From | To | Trigger |
|---|---|---|
| `queued` | `claimed` | Claim transaction (Section 9) with a new `lease_token` |
| `claimed` | `completed` | Result stored, or a result already existed (provider is skipped) |
| `claimed` | `waiting_callback` | `202` received and the fenced update succeeded |
| `claimed` | `queued` | Retryable failure within budget, circuit-open skip, or 429 pause |
| `claimed` | `held_provider` | Per-cycle failure budget or 429 limit exhausted |
| `claimed` | `failed` | Non-retryable error, or `malformed_total` reached its limit |
| `claimed` (lease expired) | `queued` | Sweeper lease reaper: `WHERE status='claimed' AND lease_expires <= now()`. The old token is kept until the next claim replaces it, so a late write from the dead worker is still fenced. Logs `lease_expired`. |
| `waiting_callback` | `completed` | Valid callback |
| `waiting_callback` | `held_provider` | `callback_deadline` passed (watchdog) |
| `held_provider` | `queued` | Recovery requeue or probe selection; starts a new cycle (Section 7) |
| any unresolved | `cancelled` | Admin rejects |

### 4.3 Ownership and fencing rules

- **Ownership** means `lease_token = :mine AND lease_expires > now()`. The `status` column describes the job; it does not decide who owns it.
- **Claim** requires `status='queued'` **and** `(lease_expires IS NULL OR lease_expires <= now())`. A row reset to `queued` while its lease is still live cannot be claimed. This is how the redelivery bug is fixed (Section 9.3).
- **Heartbeat:** `UPDATE ... SET lease_expires = now() + :d WHERE id=:id AND lease_token=:mine AND lease_expires > now()`. An expired lease can never be revived. Zero rows updated means ownership is lost, and the worker logs `lease_lost`.
- **Completion:** in one transaction, lock the media row (`SELECT ... FOR UPDATE`) and then `UPDATE screening_jobs ... WHERE id=:id AND lease_token=:mine`. Zero rows means another claim has replaced the token: roll back, record outcome `stale_discarded`, write nothing.
- **Lock order** everywhere (worker, callback, review routes, sweeper): **media row first, then job row**. This prevents deadlocks.
- In-flight HTTP is **not** cancelled when ownership is lost. The fenced commit is the guarantee. A request that has already reached the provider may still be processed and billed there.

---

## 5. Policy (pure function)

```python
PROHIBITED = ('minor', 'sexual_violence', 'bestiality', 'self_harm', 'real_person')
ALL_LABELS = PROHIBITED + ('explicit',)

@dataclass(frozen=True)
class PolicyDecision:
    decision: Literal['published', 'held', 'blocked']
    reasons: list[str]        # stable codes, e.g. "minor_block", "self_harm_review", "explicit_unverified"
    details: list[dict]       # [{label, score, frame_index, rule}]

def evaluate_policy(frames: list[dict[str, float]], workspace_verified: bool) -> PolicyDecision
```

- The policy evaluates **every frame**, and the worst outcome wins: `blocked > held > published`.
- Any prohibited label `>= 0.8` → `blocked` (`<label>_block`).
- Any prohibited label `>= 0.5` and `< 0.8` → `held` (`<label>_review`).
- `explicit >= 0.8` in an unverified workspace → `blocked` (`explicit_unverified`). In a verified workspace there is no effect.
- The input is canonical provider output: every frame has all six labels, already validated (Section 13). Defaulting an omitted label to 0 happens at the provider, which echoes input frames. The policy never sees a missing label.

The same function is used for uploads and generations, by the worker and by the callback. No thresholds appear anywhere else in production code.

**Tests:**
- Table tests for each label at 0.4999, 0.5, 0.7999, 0.8 and 1.0.
- Explicit 0.8 in verified vs unverified workspaces.
- Precedence across labels and frames (e.g. `minor=0.5` with `explicit=0.8` unverified → `blocked`).
- A 60-frame video with the bad frame at index 0, 30 and 59.
- One Hypothesis property test against `tests/helpers/policy_oracle.py`, an independent, deliberately naive implementation also used by the audit. Fixed seed in CI.

---

## 6. Provider Outcome Classification and Retry Policy

| Outcome | Detected by | Counts toward `cycle_failures` | Next step | Circuit | Limit reached |
|---|---|---|---|---|---|
| `success` | 200 + valid schema | — | Store result, apply policy | Success (reset) | — |
| `async_accepted` | 202 + valid body | — | `waiting_callback`, deadline set | Success | — |
| `timeout` | httpx connect/read timeout | **yes** | Exponential backoff + full jitter | Failure | `held_provider` |
| `conn_error` | httpx connect error | **yes** | Same | Failure | `held_provider` |
| `server_error` | 5xx | **yes** | Same | Failure | `held_provider` |
| `malformed` | 200/202 failing schema validation | **yes**, and `malformed_total += 1` | Same | Failure | `held_provider`; job `failed` once `malformed_total >= 3` |
| `rate_limited` | 429 | **no**; `cycle_rate_limited += 1` | Pause all workers until `Retry-After` (default 10 s if missing, max 60 s) | Not counted | `held_provider` once `cycle_rate_limited >= 10` |
| `in_progress` | 409 `{"status":"in_progress"}` | **no**; `cycle_rate_limited += 1` | Retry after `Retry-After` (1 s) | Not counted | Same as 429 |
| `client_error` | 422 (idempotency conflict) or another 4xx | — | No retry | Not counted | Job `failed`, media `held(screening_failed)`, alert |
| circuit-open skip | Breaker open, or 429 pause active | **no**: no call, no attempt | `next_retry_at` = end of cooldown + up to 1 s jitter. Pending media → `held(provider_unavailable)`. | — | — |

**Attempt accounting:**
- `attempt` and `job_attempts` rows: exactly one per provider HTTP call, for its whole life. These numbers match the provider's `/_stats.calls` for that media.
- `cycle_failures`: retryable failures in the current recovery cycle. The limit is `MAX_FAILURES_PER_CYCLE = 4`.
- `cycle_rate_limited`: 429 and 409 responses in the current cycle. The limit is `MAX_RATE_LIMITED_PER_CYCLE = 10`. These responses are real calls and appear in `calls`, but they don't use the failure budget, because the provider is healthy and pushing back rather than broken.
- `malformed_total`: lifetime count, never reset. A provider that keeps sending bad data is a contract bug, not an outage, so the job goes to `failed` instead of retrying forever.
- Backoff for retry *n* in a cycle: `random(0, min(BACKOFF_CAP_S, BACKOFF_BASE_S * 2^(n-1)))`.
- The media is set to `held(provider_unavailable)` on the **first** retryable failure, before retries continue.
- **Timeouts are ambiguous**: the provider may have scored. Every retry sends the same `Idempotency-Key`, so a provider that supports it returns the stored result instead of scoring again (Section 12).

429 and malformed responses can't be produced by the four provider control modes. They are tested at the worker level with an injected `httpx.MockTransport` (Section 19).

---

## 7. Circuit Breaker, Probe and Recovery

**State** lives in Redis (`cb:state`, `cb:failures`, `cb:open_until`, `cb:cooldown_s`, `cb:canary`), with an in-memory fallback.

| State | Behaviour |
|---|---|
| Closed | Calls proceed. `CB_FAILURE_THRESHOLD` consecutive failures open the circuit. |
| Open | No provider calls. Workers that claim a job do a circuit-open skip (Section 6). |
| Half-open | Reached once `now >= cb:open_until`. The first worker to win `SET cb:canary NX EX` makes **one** call with the job it holds. All other workers keep skipping. Success → closed: counters reset, cooldown back to base. Failure → open again with doubled cooldown (`CB_COOLDOWN_S` up to `CB_COOLDOWN_MAX_S`). |

**Sweeper.** It runs in the worker containers. Exactly one leader at a time, chosen by `pg_try_advisory_lock`, every `SWEEP_INTERVAL_S`:

1. **Lease reaper:** `claimed` jobs whose lease has expired → `queued`, with `next_retry_at = now()`. The token is unchanged, so the claim query (Section 9.1) can pick the job up, and the next claim's new token fences the dead worker. Logs `lease_expired` and increments the `lease_expirations` metric. This is the only path by which a crashed worker's job is recovered.
2. **Callback watchdog:** `waiting_callback` jobs past `callback_deadline` → `held_provider`, with `last_error_class='callback_timeout'`. The media stays held.
3. **Probe (fixes the Revision 1 deadlock):** this runs when:
   - the circuit is open,
   - the cooldown has elapsed,
   - no `queued` job is due (none could serve as the canary),
   - and at least one `held_provider` job exists.

   Then the sweeper requeues **one existing** `held_provider` job: the one with the oldest `updated_at`, so probes rotate across jobs and workspaces. That job starts a new cycle, and the next worker uses it as the canary. **No synthetic or fake media ID is ever sent to the provider.**
4. **Recovery requeue:** while the circuit is closed, every `held_provider` job whose `updated_at < now() - HELD_PROVIDER_MIN_WAIT_S` is requeued, starting a new cycle. The minimum wait stops a flaky but technically closed provider from causing a requeue storm.

**Starting a new cycle:** `cycle += 1`, `cycle_failures = 0`, `cycle_rate_limited = 0`, `next_retry_at = now()`, `status = 'queued'`. The lifetime `attempt` and `malformed_total` are never reset. There is **no** limit on cycles, because a 40-minute outage has to be survivable. Probe cost is bounded by the cooldown: at most one call per cooldown period, and the cooldown backs off to 60 s. The workload reports probe calls inside "provider calls per upload".

**During an outage:** after `CB_FAILURE_THRESHOLD` failures, most jobs never reach `held_provider`. They stay `queued` and are skipped without consuming attempts, and their media is `held(provider_unavailable)`. Whichever job is due when the cooldown ends becomes the canary. Once the circuit closes, the queued jobs proceed under fair scheduling, and the sweeper requeues the `held_provider` ones. Media held for outage reasons is re-screened without a human.

---

## 8. Configuration Defaults

All values are environment variables, documented in `.env.example`. Test values live in `docker-compose.test.yml`.

**Rules the values must satisfy:**
- slow delay < read timeout < lease duration;
- heartbeat ≤ lease / 3.

| Variable | Default | Test |
|---|---:|---:|
| `PROVIDER_CONNECT_TIMEOUT_S` | 2 | 1 |
| `PROVIDER_READ_TIMEOUT_S` | 10 | 3 |
| Provider slow delay (`/_control` `delay_ms`, default) | 3000 ms | 1000 ms (set by tests) |
| `PROVIDER_HANG_S` (provider `timeout` mode, bounded) | 60 | 30 |
| `WORKER_CONCURRENCY` (per container), replicas | 4, 2 | 4, 2 |
| `MAX_CONCURRENT_PER_WORKSPACE` | 4 | 2 |
| `LEASE_DURATION_S` / `LEASE_HEARTBEAT_S` | 30 / 5 | 6 / 1 |
| `MAX_FAILURES_PER_CYCLE` | 4 | 4 |
| `MAX_RATE_LIMITED_PER_CYCLE` | 10 | 10 |
| `MAX_MALFORMED_TOTAL` | 3 | 3 |
| `BACKOFF_BASE_S` / `BACKOFF_CAP_S` | 1 / 8 | 0.2 / 1 |
| `RETRY_AFTER_DEFAULT_S` / `RETRY_AFTER_MAX_S` | 10 / 60 | 1 / 5 |
| `CB_FAILURE_THRESHOLD` | 5 | 3 |
| `CB_COOLDOWN_S` / `CB_COOLDOWN_MAX_S` | 10 / 60 | 2 / 8 |
| `HELD_PROVIDER_MIN_WAIT_S` | 30 | 3 |
| `CALLBACK_DEADLINE_S` | 60 | 8 |
| `SWEEP_INTERVAL_S` / `WORKER_POLL_INTERVAL_S` | 2 / 2 | 0.5 / 0.5 |
| `OUTAGE_STATUS_TRANSITION_S` (pending → held once the worker can see it) | 5 | 3 |
| `MAX_FRAME_COUNT` / `MAX_BODY_BYTES` | 3600 / 1 MB | same |
| Provider `ASYNC_FRAME_THRESHOLD` | 30 | 30 |
| Provider `CALLBACK_DELAY_MS` / `CALLBACK_MAX_ATTEMPTS` | 500 / 5 | 200 / 5 |
| `PROVIDER_CALLBACK_SECRET` | see Section 12.4 | same |

---

## 9. Fair Scheduling and the Workspace Cap

### 9.1 Claim transaction

```sql
BEGIN;
-- 1. Candidate: interleave workspaces by per-workspace rank; skip capped workspaces.
WITH live AS (
    SELECT workspace_id, COUNT(*) AS n FROM screening_jobs
    WHERE lease_expires > now() AND status IN ('claimed','queued')
    GROUP BY workspace_id
), ranked AS (
    SELECT j.id, j.workspace_id, j.created_at,
           ROW_NUMBER() OVER (PARTITION BY j.workspace_id ORDER BY j.created_at, j.id) AS rn
    FROM screening_jobs j LEFT JOIN live l ON l.workspace_id = j.workspace_id
    WHERE j.status = 'queued'
      AND (j.next_retry_at IS NULL OR j.next_retry_at <= now())
      AND (j.lease_expires IS NULL OR j.lease_expires <= now())
      AND COALESCE(l.n, 0) < :cap
)
SELECT j.id, j.workspace_id, j.media_id
FROM screening_jobs j JOIN ranked r ON r.id = j.id
ORDER BY r.rn, r.created_at, r.id
LIMIT 1
FOR UPDATE OF j SKIP LOCKED;

-- 2. Serialize cap checks per workspace, then recount under the lock.
SELECT 1 FROM workspaces WHERE id = :ws FOR NO KEY UPDATE;
SELECT COUNT(*) FROM screening_jobs WHERE workspace_id = :ws AND lease_expires > now()
   AND status IN ('claimed','queued');
-- 3. If still below the cap: status='claimed', lease_token=gen_random_uuid(),
--    lease_expires=now()+:lease. Otherwise roll back and retry after a short delay.
COMMIT;
```

- **Live leases** are counted whatever the status, so a job reset to `queued` while its lease is live still occupies its slot (Section 9.3).
- `FOR NO KEY UPDATE` doesn't conflict with the `KEY SHARE` locks taken by foreign-key checks, so media inserts are never blocked.
- `waiting_callback` jobs have `lease_expires = NULL` and don't use a worker slot.

**Order of claims with two workspaces:** `A[1], B[1], A[2], B[2], …`, recomputed on every claim.

### 9.2 The workspace cap is a deliberate trade-off (also in DECISIONS.md)

- **Why it exists:** ranking alone decides who goes *next*. It doesn't stop workspace A from filling all 8 worker tasks with slow calls, timing out at up to 10 s each, while B's image waits for a slot. A cap of 4 per workspace with 8 tasks keeps capacity free for another workspace, so B's wait is bounded by the poll interval rather than by A's job duration.
- **What it costs:** the cap isn't work-conserving. A workspace bursting alone uses at most 4 of the 8 tasks, so half the capacity sits idle with a single tenant.
- **Rejected alternatives:**
  - A work-conserving cap that only applies when other workspaces have eligible jobs: more SQL, harder to test.
  - Weighted fair queuing: unnecessary for two tenants.
- **When to revisit:** when there are many tenants or one workspace has high load. The cap should then become a per-workspace share of total capacity.
- **Scope:** fairness here means bounded *claim* opportunity. During a total outage nobody gets a successful result. Fairness then means both workspaces' media reaches `held(provider_unavailable)` promptly, and both drain after recovery.

### 9.3 Redelivery (incident bug 4)

The harness (Section 19.3) runs `UPDATE screening_jobs SET status='queued' WHERE media_id=:m` while the original provider call is still in progress in slow mode. It uses only the columns shared by both tags.

- **Baseline:** another worker task claims the row by `status` alone and calls the provider a second time. `calls == 2`, `scored == 2`.
- **Fixed:** the lease and token are left untouched, so the claim query excludes the row (live lease), and the cap counts it. The original worker's heartbeat and completion check only the token, not `status`, so they succeed. Result: `calls == 1`, one result, job `completed`.

---

## 10. Observability

**Structured JSON logs.** stdlib `logging` with a small JSON formatter, one event per line. Fields are present where applicable:
- **IDs:** `request_id`, `workspace_id`, `user_id`, `media_id`, `job_id`, `attempt`, `cycle`, `lease_token`, `provider_request_id`
- **Outcome:** `event`, `provider_outcome`, `http_status`, `from_status`, `to_status`, `decision`, `reason_codes`, `latency_ms`

`grep <media_id>` shows a media item's whole history.

| Event | Emitted when |
|---|---|
| `media_accepted` / `prompt_refused` | `POST /media` |
| `job_claimed`, `provider_call`, `provider_outcome` | Worker |
| `retry_scheduled`, `budget_exhausted`, `circuit_skip` | Worker |
| `media_transition`, `result_stored`, `stale_discarded`, `lease_lost` | Worker |
| `lease_expired`, `callback_timeout`, `probe_requeued`, `recovery_requeued` | Sweeper |
| `circuit_opened`, `circuit_half_open`, `circuit_closed` | Breaker |
| `callback_accepted`, `callback_rejected` (with reason), `callback_duplicate`, `callback_ignored_terminal` | Callback |
| `review_action` | Review routes |
| `job_failed` (level ERROR, `alert=true`) | Worker |

**Never logged:** raw prompts (a refused prompt logs `prompt_sha256` and the matched rule codes), identity headers, `X-Signature`, secrets, raw callback bodies.

**`GET /metrics`** (JSON, internal):
- `jobs_by_status{workspace}`
- `oldest_unresolved_job_age_s{workspace}`: `now() - MIN(created_at)` over status in **`queued, claimed, waiting_callback, held_provider`**
- `failed_jobs{workspace}`, plus `oldest_failed_job_age_s`. The README alert rule is `failed_jobs > 0`.
- `held_media{workspace, hold_reason}`
- `provider_calls_total` and outcome counts from `job_attempts`
- `retries_scheduled`, `budget_exhausted`, `lease_expirations`, `stale_discarded`
- `circuit_state`
- callback rejected and duplicate counts (Redis, best effort)

Also `GET /healthz` (liveness) and `GET /readyz`. `/readyz` checks the database and migration head. A Redis or provider outage shows up as degraded but does not make the API unready, because uploads are still accepted and held.

The workload script (Section 17) computes its backlog metric from shared columns so it also works at the baseline:

```sql
SELECT workspace_id, EXTRACT(EPOCH FROM now() - MIN(created_at)) FROM screening_jobs
WHERE status NOT IN ('done','completed','failed','cancelled') GROUP BY workspace_id;
```

---

## 11. Authentication and Authorization

`X-User-Id` and `X-Workspace-Id` are a **test-harness identity mechanism, not production authentication**. The README says so.

These checks run in order, **before any media lookup**, so a 401 or 403 reveals nothing about media:

| Check | Response |
|---|---|
| A header is missing | `401 {"detail":"Authentication required"}` |
| User unknown, or not in the stated workspace | `403 {"detail":"Forbidden"}` |
| Admin-only route (`/review*`) called by a member | `403 {"detail":"Admin access required"}` |
| Media not in the caller's workspace, nonexistent, or a malformed UUID | `404 {"detail":"Not found"}` |

- **Access to media:** any member of the owning workspace can read it, whoever uploaded it. Callers from another workspace get `404`.
- **Tenant scoping in queries:** every media and review query filters with `WHERE id = :id AND workspace_id = :caller_ws`. Records are never fetched first and filtered afterwards.
- **No identity headers on the callback:** `POST /provider/callback` is authenticated only by HMAC.
- **CORS** allows only the frontend origin.

---

## 12. Fake Provider (separate service, port 8001)

### 12.1 Control and stats (external contract)

`POST /_control` accepts `{"mode": "ok" | "slow" | "timeout" | "error", "delay_ms"?: int}`. `"fail"` is accepted as an alias of `"error"`. Any other mode → `422`. Response: `200 {"mode": "<canonical>"}`.

| Mode | `/score` behaviour |
|---|---|
| `ok` | Responds immediately |
| `slow` | Scores on receipt, then waits `delay_ms` (default 3000) before responding |
| `timeout` | Never responds within the client timeout. It holds the connection for `PROVIDER_HANG_S` and then closes it. Nothing is scored. |
| `error` / `fail` | `500 {"error":"provider_error"}`. Nothing is scored. |

`GET /_stats`:

```json
{"calls": {"<media id>": 1}, "scored": {"<media id>": 1}, "callback_sends": {"<media id>": 1}}
```

- `calls`: every `/score` request received, whatever the outcome. This is the required contract and is never adjusted.
- `scored`: real scoring executions, excluding idempotent replays.
- `callback_sends`: callback delivery attempts.

Stats are in memory. They are not reset during a test run or workload.

### 12.2 `POST /score` (worker → provider)

Request headers: `Content-Type: application/json`, `Idempotency-Key: <media_id>`.

```json
{"media_id": "<uuid>",
 "frames": [{"explicit": 0.02}, {"explicit": 0.95, "self_harm": 0.1}],
 "callback_url": "http://api:8000/provider/callback"}
```

| Case | Response |
|---|---|
| ≤ 30 frames | `200 {"media_id", "request_id", "frames": [{all six labels}, ...]}`. Every frame is echoed, and omitted labels become `0.0`. |
| > 30 frames | `202 {"media_id", "request_id", "status": "accepted"}`. The callback is scheduled after `CALLBACK_DELAY_MS`. |

### 12.3 Idempotency (by `Idempotency-Key`; requests without the header are never deduplicated)

The provider stores `{payload_sha256, request_id, result}` per key. A result is stored only when scoring actually ran (`ok`, `slow`).

| Case | `calls` | `scored` | Response |
|---|---|---|---|
| New key | +1 | +1 | Normal 200 / 202 |
| Same key and payload, sync result stored | +1 | 0 | `200` with the stored result (same `request_id`), after the slow delay in slow mode |
| Same key and payload, async result stored | +1 | 0 | `202` again. **The callback of the stored result is re-scheduled** (`callback_sends` increases). This is how a job recovers from a lost callback without paying for scoring again. |
| Same key, still being scored concurrently | +1 | 0 | `409 {"status":"in_progress"}` with `Retry-After: 1` |
| Same key, different payload | +1 | 0 | `422 {"error":"idempotency_key_reused"}` |
| Any request in `timeout` / `error` mode | +1 | 0 | As in the mode table; no key is stored |

The baseline worker sends no `Idempotency-Key`, so it demonstrates duplicate scoring. **Disclaimer (README):** the fake provider's idempotency demonstrates the pattern. It does not prove that a real moderation vendor supports idempotency keys, or how long it keeps them.

### 12.4 Callback (provider → API) and signature

```
POST {callback_url}
Content-Type: application/json
X-Signature: <lowercase hex HMAC-SHA256(PROVIDER_CALLBACK_SECRET, raw_body_bytes)>

{"media_id": "<uuid>", "request_id": "<provider id>", "frames": [{all six labels}, ...]}
```

- The API reads the raw body bytes **before** parsing, strips an optional `sha256=` prefix, and compares with `hmac.compare_digest`. A missing, invalid or non-hex signature → `401`, with nothing written.
- **Delivery retries:** the provider retries on network errors and any non-2xx response. It makes up to `CALLBACK_MAX_ATTEMPTS = 5` attempts with backoff of 0.5, 1, 2 and 4 s, and stops at the first 2xx. Each attempt increments `callback_sends`.
- **Secret:** read from the environment by both services. `.env.example` contains only a placeholder. `docker-compose.yml` uses `${PROVIDER_CALLBACK_SECRET:-local-dev-placeholder-not-secret}` so that a fresh `docker compose up` works. The API logs a WARNING at startup when the placeholder is in use. The secret is never logged.

### 12.5 Callback handling (API, one transaction)

1. Verify the HMAC over the raw bytes; failure → `401`.
2. Parse against the strict callback schema (Section 13). Invalid → `422`, nothing written.
3. `SELECT media ... FOR UPDATE`. Unknown media → `200`, nothing written (stops provider retries and doesn't leak whether the media exists).
4. Frame count differs from `media.frame_count` → `422`.
5. A result already exists → `200`, `callback_duplicate`, no change.
6. Media is not `pending` or `held(provider_unavailable | screening_failed)` (e.g. already `blocked` by an admin) → `200`, `callback_ignored_terminal`, no change. An admin rejection is never undone.
7. Run `evaluate_policy`, insert `screening_results`, update the media status (checked by `media_guard`), then set the job to `completed` and clear `lease_token` and `lease_expires`. This fences any worker still holding the old token. Commit → `200`.

| Race | Result |
|---|---|
| Callback arrives before the worker commits `waiting_callback` | The callback completes the job and clears the token. The worker's fenced update affects 0 rows, so it discards. |
| Duplicate or replayed callback | One result row. Repeats return `200` with no change. |
| Late callback after the watchdog moved the job to `held_provider` | Accepted. The result is stored and the job completed; the media is still eligible. |
| Late callback after admin rejection | No change (step 6). |

Replay protection beyond idempotency (timestamps, nonces) isn't added, because the provider contract doesn't include those headers. A replayed valid body can only repeat a decision that already exists. Documented as a limitation.

---

## 13. Validation

**`POST /media` (Pydantic v2, strict):**
- `type ∈ {image, video}` and `source ∈ {upload, generation}`.
- `prompt` is required for `generation`.
- `frames` is a non-empty list of objects. An image has exactly one frame; a video has 1 to `MAX_FRAME_COUNT` frames.
- Each frame's keys are a subset of the six labels. **An unknown label → `422`.**
- Values are JSON numbers in `[0, 1]`. Booleans, NaN, infinities and strings are rejected.
- Bodies larger than `MAX_BODY_BYTES` → `413`.
- Every validation error returns `422 {"reasons": [...]}`, through a custom `RequestValidationError` handler.

**Provider responses and callbacks:**
- `frames` has exactly `media.frame_count` elements.
- Every frame has exactly the six labels, with numeric values in `[0, 1]`.
- `media_id` matches the request.
- Anything else counts as `malformed`. Callback schema rules are the same, with extra top-level keys forbidden.

---

## 14. Generation Prompt Screening

The interface is `screen_prompt(text) -> PromptVerdict(allowed: bool, reasons: list[str])`. Behind it is a deterministic local implementation, which a classifier could replace or add to (README Part 3). It runs **before anything is stored or sent to a provider**. A refusal → `422 {"reasons": ["prompt_minor", ...]}`, and the log records `prompt_sha256` plus the rule codes, never the prompt text.

**Normalization.** Two views of the text are produced:
- **View A**, used for age parsing:
  1. Unicode NFKC.
  2. Remove zero-width and format characters.
  3. Fold confusable/homoglyph characters (a small table).
  4. Lowercase.
  5. Collapse whitespace.
  6. Parse numeric and spelled-out ages: `16`, `16yo`, `16 y/o`, `sixteen-year-old`, `aged 15`.
- **View B**, used for keyword rules: View A, then leetspeak substitution (`0→o`, `1→i`, `3→e`, `4→a`, `5→s`, `7→t`, `@→a`, `$→s`), then collapsing runs of single characters separated by spaces or punctuation (`s.c.h.o.o.l g i r l` → `schoolgirl`).

Ages are parsed **only on View A**, before leetspeak, so that `16` isn't turned into `ie`. Keywords match on **whole words**, so "nineteen" never matches `teen`.

**Rule codes:**
- `prompt_minor`: an age below 18 next to a person word; `child`, `kid`, `minor` (person context), `underage`, `preteen`, `teen`, `teenager`, `schoolgirl`, `schoolboy`, `loli`, `barely legal`, `looks young`, `young-looking`, `sweet sixteen`.
- `prompt_sexual_violence`, `prompt_bestiality`, `prompt_self_harm`, `prompt_incest`, `prompt_trafficking`: a small keyword list each.
- `prompt_real_person`: `deepfake`, `face swap`, `looks like <Name>`, and a small configurable list of celebrity names.

The rule is conservative: a minor described in any context is refused, which knowingly accepts some false positives.

**Tests:**
- **Refused:**
  - "a 16-year-old on a beach", "sixteen year old", "16yo", "a teen"
  - "s c h o o l g i r l", "5ch00lgirl", a homoglyph "tееn" (Cyrillic е)
  - a zero-width-split "under​age"
  - one prompt per other category, and "face swap with Taylor Swift"
- **Allowed:**
  - "two adults, both 25, on a beach", "a 19-year-old woman", "an eighteen year old couple", "nineteen candles"
  - "a 16 oz coffee", "a minor chord progression", "cutting vegetables for dinner", "forced perspective photo"

**Limitations (README):** keyword rules miss euphemisms, indirect references and new slang, and they misfire on innocent context. Production needs the layered design described in Part 3.

---

## 15. API Contract

| Route | Success | Errors |
|---|---|---|
| `POST /media` | `202 {"id", "status": "pending"}` | `422 {"reasons"}` (prompt refused or validation), `401`, `403`, `413` |
| `GET /media/{id}` | `200 {"id", "status", "reasons"}` | `401`, `403`, `404` |
| `GET /review` (admin) | `200 [{"id", "status", "reasons", "hold_reason", "reviewable", "type", "source", "created_at"}]`; held media of the caller's workspace, oldest first | `401`, `403` |
| `POST /review/{id}/approve` (admin) | `200 {"id", "status": "published"}` | `403` member; `404`; `409 {"detail": "not_held"}`; `409 {"detail": "not_screened"}` for `provider_unavailable` / `screening_failed` |
| `POST /review/{id}/reject` (admin) | `200 {"id", "status": "blocked"}`; an unresolved job → `cancelled` | `403`, `404`, `409 {"detail": "not_held"}` |
| `POST /provider/callback` | `200` | `401` bad or missing signature; `422` invalid signed body |
| `GET /healthz`, `/readyz`, `/metrics` | `200` | `503` from `/readyz` when the database is unavailable |

- `reasons` on media is a list of stable codes: policy codes, `provider_unavailable`, `screening_failed`, `admin_approved`, `admin_rejected`.
- Approve and reject lock the media row. They are effectively idempotent: a repeat gets `409 not_held`, and a simultaneous approve and reject means one wins and the other gets `409`.
- Unhandled errors return `500 {"detail": "Internal server error"}` with no stack trace. A failed database query never returns an empty list.

---

## 16. Broken Baseline

### 16.1 The five incident bugs (naive code at tag `broken-baseline`)

| # | Bug in the naive handler | Symptom from the incident |
|---|---|---|
| 1 | A provider timeout or exception is treated as a pass, and the media is published | Uploads went live unchecked during the outage |
| 2 | Only `frames[:8]` is sent to the provider and evaluated | Explicit content in the last minute of a video was published in an unverified workspace |
| 3 | `screen_prompt` is never called | A prompt for a 16-year-old reached the generator |
| 4 | Claims use `status` alone, with no lease or idempotency key | Some uploads were scored 2–3 times |
| 5 | The UI's `catch` renders `[]`, showing "Nothing to review" | Moderators saw an empty queue while 40 items waited |

The baseline is otherwise a complete, runnable naive system: naive schema `0001`, the same provider, the same routes, a naive worker with `WORKER_CONCURRENCY` tasks, and the naive UI. The naive callback does verify signatures, because that isn't one of the incident bugs.

### 16.2 Commit sequence

```
c1  scaffold: compose, Dockerfiles, lockfiles, naive schema 0001, seed, fake provider (complete)
c2  naive API + worker + UI containing bugs 1–5
c3  incident regression tests + Vitest queue test + workload script + audit checks A/B
c4  evidence/before/ (real output)                                      ← tag: broken-baseline
c5  0002_hardening: results/attempts/reviews tables, lease columns, partial index, triggers
c6  fix 1: fail closed on provider failure (classification, held(provider_unavailable))
c7  fix 2: screen every frame; strict provider response validation
c8  fix 3: prompt screening before persistence
c9  fix 4: lease + fencing + Idempotency-Key; claim excludes live leases
c10 fix 5: UI distinguishes loading / failed / empty
c11 reliability: retry budgets, circuit breaker, sweeper (probe, recovery, watchdog), async path, fairness cap
c12 observability: JSON logs, /metrics, audit check C
c13 evidence/after/, crash test, docs
```

Each fix commit turns its regression test green. The default branch has **no** runtime toggle that brings back any baseline behaviour. A reviewer reproduces a bug with `git worktree add ../baseline broken-baseline`, then runs the same test command there (Makefile `repro-bugN`).

---

## 17. Workload and Measurements (`scripts/workload.py`)

The workload uses a seeded random generator and a fixed composition, and the same parameters at both tags. It talks only to the HTTP API, the provider's `/_control` and `/_stats`, and shared database columns.

**Composition:**
- **Workspace A = `ws_verified` (burst):** 200 videos per burst, `--video-frames 20` by default. Of these:
  - 5%: `minor = 0.95` at frame 15 → expected `blocked`
  - 5%: `self_harm = 0.6` at the last frame → expected `held`
  - 20%: `explicit = 0.95` → expected `published` (verified workspace)
  - the rest clean
- **Workspace B = `ws_unverified` (trickle):** one image per second, 20% of them with `explicit = 0.95` (expected `blocked`). Also:
  - 5 videos of 20 frames with `explicit = 0.95` in the last frame (the incident video) → expected `blocked`
  - 5 prohibited generation prompts (expected `422`) and 5 safe ones

**Phases:**

| Phase | Provider mode | A | B |
|---|---|---|---|
| 1. Normal | `ok` | Burst of 200 at t=0 | Trickle for 20 s |
| 2. Outage | `timeout` (the incident failure) | Burst of 200 at phase start | Trickle for 60 s |
| 3. Recovery | `ok` | — (phase 2 backlog drains) | Trickle until everything resolves or 180 s pass |

**40-frame variant:** `--video-frames 40` runs the same workload with async videos (202 then callback). It exercises the callback path, the deadline and the duplicate callbacks under load. At the baseline, bug 2 sends only 8 frames, so the baseline never takes the async path. The report says so.

**Measured and saved** as `evidence/{before,after}/workload[_40].json` plus console output:

| Metric | Method |
|---|---|
| **Media published unchecked** | `scripts/audit.py`: check A (`/_stats.scored[id] == 0`) ∪ check B (oracle on input frames says `held`/`blocked`) ∪ check C (fixed only). The three are reported separately. |
| Prohibited prompts accepted | Count of `202` responses to the 5 known-bad prompts |
| **Provider calls per upload** | `sum(calls) / accepted uploads`, also `sum(scored) / uploads`, max calls for one media, number of media with `scored > 1`, probe calls (fixed) |
| **Oldest waiting job age** | The query in Section 10, sampled every 1 s, per workspace. Reported as a time series plus the maximum per phase, and the time for the backlog to drain after recovery. |
| B fairness | B's image time from acceptance to first claim (p50/p95/max) and to final status, per phase, compared with A's |
| Outage hold latency | Time for B's and A's media to go from `pending` to `held` during phase 2 (fixed). Budget: `OUTAGE_STATUS_TRANSITION_S`. |

**Caveat:** the baseline's "oldest job age" looks healthy because it finishes jobs without screening them. That is why it is reported next to published-unchecked and is never taken as proof of screening.

**Units and limits:** times are seconds, measured on one laptop running Docker Desktop. These are not production throughput numbers. Expected results are not written down in advance; the README tables are filled only from saved files.

---

## 18. Worker Crash After Scoring, Before Saving

**Script:** `scripts/crash_test.py` (host). It writes `evidence/after/crash_test.txt`.

1. Set provider `slow` with `delay_ms=8000`. The fake provider scores on receipt, so `scored = 1` before it responds.
2. Upload one 20-frame video, and poll `/_stats` until `scored[id] == 1`.
3. `docker compose kill -s SIGKILL worker`, which kills every replica.
4. Assert the media is not `published`.
5. Set the mode back to `ok` and run `docker compose up -d worker`. After the lease expires (up to `LEASE_DURATION_S`), the sweeper's lease reaper requeues the job, which is then claimed again and calls `/score` with the same `Idempotency-Key`, and the provider returns its stored result.
6. Assert the final status is correct, `calls == 2`, `scored == 1`, there is exactly one `screening_results` row, and `job_attempts` shows the first attempt still unfinished and the second `success`.

There is also an in-process pytest test: monkeypatch the persistence function to raise after the provider response, then run a second worker task. The patch lives only in the test, not in production code.

**What it proves:** a worker that dies between the provider response and the commit:
- never publishes anything;
- leaves the job recoverable through lease expiry;
- recovers with no second *scoring*, because the provider honours the key.

**What it doesn't prove:**
- behaviour on machine or power loss, PostgreSQL crash or fsync semantics, network partitions, or clock skew between workers;
- that a real vendor supports idempotency keys, or for how long it keeps them;
- whether a vendor bills replays.

Without provider idempotency, the second call is a second charge. The README explains the mitigations:
- vendor idempotency keys;
- a durable handoff, where results are written to a durable inbox or queue by the provider, or by an edge service, before processing;
- daily reconciliation of vendor billing exports against `job_attempts`.

---

## 19. Test Plan (focused)

### 19.1 Unit tests (no Docker)

| File | Covers |
|---|---|
| `test_policy.py` | Boundary table (5 labels × 5 values), explicit vs verification, precedence, frame index 0/30/59, Hypothesis vs oracle |
| `test_prompt_screen.py` | Refused and allowed lists from Section 14 |
| `test_validation.py` | Unknown label → 422, NaN/inf/bool/out-of-range, image frame count, provider response and callback schemas |
| `test_signature.py` | Valid, tampered body, altered whitespace, wrong secret, missing, `sha256=` prefix, non-hex |
| `test_classification.py` | `MockTransport`: timeout, connect error, 500, 429 with and without `Retry-After`, 409, malformed, 422; budget counters per Section 6 |
| `test_circuit.py` | Opens at threshold; open skips without an attempt; half-open makes exactly one canary call; cooldown doubles; 429 doesn't count |

### 19.2 Integration tests (compose stack plus `docker-compose.test.yml`; run serially because provider mode is global)

| Test | Covers |
|---|---|
| `test_post_media_202_shape`, `test_get_media_shape` | Contract shapes |
| `test_member_of_owning_ws_reads_others_upload` | `member_v` reads media uploaded by `admin_v` → 200 |
| `test_cross_workspace_404` | `admin_u` reading `ws_verified` media → 404; same for review approve and reject |
| `test_identity_401_403` | Missing headers → 401; user/workspace mismatch → 403 |
| `test_member_approve_403`, `test_member_review_list_403` | Admin-only routes |
| `test_hold_range_score_held` | `self_harm = 0.6` → `held(needs_human_review)`, shown in `/review` as reviewable |
| `test_explicit_verified_vs_unverified` | Published in one workspace, blocked in the other |
| `test_approve_held_200`, `test_reject_held_200`, `test_approve_outage_hold_409`, `test_approve_twice_409` | Review semantics |
| `test_unsigned_callback_401`, `test_bad_signature_401`, `test_signed_malformed_422` | Callback security |
| `test_async_video_callback` | 40 frames → 202 → callback → final status; `calls == 1` |
| `test_duplicate_callback_idempotent`, `test_late_callback_after_reject` | One result; rejection not undone |
| `test_callback_deadline_recovers` | Callback blocked at the API (test override returns 503 on the callback route) → watchdog → `held_provider` → requeue → provider re-sends the stored result; `scored == 1` |
| `test_retry_exhaustion_held_provider` | `error` mode → `held(provider_unavailable)` after the first failure; job `held_provider`; never published |
| `test_outage_recovery_auto_rescreen` | `error` → probe → `ok` → media re-screened with no human involved; probe used a real held job |
| `test_circuit_open_no_calls` | While open, `calls` doesn't increase except for the canary |
| `test_stale_worker_discarded` | Expire the lease while worker A is in slow mode; B claims; A's commit is discarded; one result; `calls == 2`, `scored == 1` |
| `test_cap_and_skip_locked` | Slow burst in one workspace, cap 2, 8 tasks: peak live leases ≤ 2 (sampled), and an image from the other workspace is claimed while the cap is full |
| `test_reject_cancels_job` | Rejecting an outage-held item → job `cancelled`, and no further `calls` |
| `test_metrics_backlog` | `oldest_unresolved_job_age_s` is above 0 during an outage with jobs in `held_provider`/`queued`; `failed_jobs` reflects a forced 422 |
| `test_logs_traceable` | Logs captured for one media include the required fields and no prompt or signature |

### 19.3 Incident regression tests (black-box, `tests/incident/`, identical at both tags)

| Test | Bug | Assertion (baseline FAILS, fixed PASSES) |
|---|---|---|
| `test_timeout_does_not_publish` | 1 | `timeout` mode, clean image. After the job leaves the active state, or after 20 s, the status is not `published`. At the fixed version it is `held`. |
| `test_late_frame_is_screened` | 2 | Unverified workspace, 20-frame video with `explicit = 0.95` at frame 19 → `blocked`. Verified, `minor = 0.95` at frame 15 → `blocked`. |
| `test_prohibited_prompt_refused` | 3 | Generation with "a 16-year-old ..." → `422` with `reasons`; no `/_stats` entry |
| `test_redelivery_while_running` | 4 | `slow` 3000 ms. Once `/_stats.calls[id] == 1` (the call is in flight), the harness runs `UPDATE screening_jobs SET status='queued' WHERE media_id=:id`. After resolution: `calls[id] == 1`, `scored[id] == 1`, and one consistent final status. |
| `ReviewQueue.test.tsx: failed request never shows "Nothing to review"` | 5 | Mock fetch → 500 gives the error state with Retry, and the text "Nothing to review" is absent |

"Finished job is not proof" is enforced in test 1 and in the audit: assertions look at media status and provider evidence, never at job status alone.

### 19.4 SQL trigger tests (PostgreSQL 16, direct SQL, about 8)

1. INSERT media with status `published` fails.
2. Publish with no result fails.
3. Publish with partial coverage (`frames_scored < frame_count`) fails.
4. Publish when the result decision is `held`, with no approval row, fails. Adding an admin approval succeeds. A member's approval row still fails.
5. `blocked → published` and `published → held` fail.
6. `held(needs_human_review)` without a `held` result fails. `held(provider_unavailable)` without a result succeeds.
7. UPDATE or DELETE on `screening_results` or `media_reviews` fails. A second result for the same media fails (primary key).
8. Positive controls: automated publish and automated block with valid results succeed.

### 19.5 Frontend (Vitest + React Testing Library)

Loading state; error state with Retry, which never shows the empty message; genuinely empty; data with approve/reject; a failed approve shows an error and keeps the item; `reviewable = false` disables Approve.

### 19.6 Evidence files

| Location | Contents |
|---|---|
| `evidence/before/` | `incident_tests.txt`, `frontend_tests.txt`, `workload.json`, `workload_40.json`, `audit.txt` |
| `evidence/after/` | The same files, plus `full_suite.txt`, `crash_test.txt`, `sample_trace.log` (one media through outage and recovery) |
| `evidence/fresh_clone.txt` | Output of the fresh-clone check |

Everything is real command output. Nothing is edited by hand.

---

## 20. Repository Layout

```
./
├── docker-compose.yml, docker-compose.test.yml, .env.example, .gitignore, Makefile
├── README.md, AI.md, DECISIONS.md, PLAN.md
├── .github/workflows/ci.yml           # ruff + unit tests + frontend tests; compose integration job
├── api/
│   ├── Dockerfile, pyproject.toml, uv.lock, alembic.ini
│   ├── alembic/versions/0001_naive.py, 0002_hardening.py
│   ├── app/
│   │   ├── main.py, config.py, db.py, models.py, schemas.py, auth.py, seed.py, logging.py
│   │   ├── policy.py, prompt_screen.py, provider_client.py, circuit.py
│   │   ├── routes/ media.py, review.py, callback.py, ops.py
│   │   └── worker/ main.py, claim.py, process.py, sweeper.py
│   └── tests/ unit/, integration/, incident/, sql/, helpers/policy_oracle.py, conftest.py
├── provider/ Dockerfile, pyproject.toml, uv.lock, app/main.py
├── frontend/ Dockerfile, package.json, package-lock.json, vite.config.ts, src/{App,ReviewQueue,api}.tsx, src/ReviewQueue.test.tsx
├── scripts/ workload.py, audit.py, crash_test.py
├── evidence/ before/, after/
└── docs/WALKTHROUGH.md
```

- **Tooling:**
  - Python: `uv` with `uv.lock`, and `ruff` for linting.
  - `mypy` only if time allows (Section 22).
  - Frontend: `npm` with `package-lock.json`.
- **Makefile:** every target is a one-liner that the README also lists in full, because `make` may not exist on Windows.
- **Documentation:**
  - README.md holds the architecture summary, the requirement-status table, Part 3, and the production plan.
  - DECISIONS.md holds the ADRs.
  - docs/WALKTHROUGH.md is interview preparation, written last.
- **Private files and the docs folder:**
  - **Before `git init`:** keep private working notes outside the repository, and move this plan to `./PLAN.md`. `Docs/` and `docs/` are the same folder on Windows.

---

## 21. README Content (required by the assignment)

1. Quick start (`docker compose up`; ports; how to run the tests, workload and crash test).
2. **The incident:** each failure, mapped to its bug, with the baseline evidence.
3. **The fix:** a short design summary (queue, fencing, fail-closed, guard trigger and trust boundary, fairness and cap trade-off).
4. **Before/after table:** filled only from `evidence/` files.
5. Retry policy table (Section 6) and circuit/recovery summary.
6. **Crash test:** what it proves and what it doesn't.
7. **Remaining limits:**
   - no durable callback inbox;
   - no replay nonce;
   - the provider idempotency caveat;
   - keyword prompt screening;
   - the non-work-conserving cap;
   - single-node measurements.
8. **Requirement status:** each item marked verified, partially verified or not verified, with its evidence.
9. **Time spent:** actual.
10. **Part 3:**
    - provider choice and cost: paid API vs self-hosted vs hybrid; coverage of all five prohibited labels plus explicit; the 10k images + 1k one-minute videos estimate with **cited official pricing, access date and assumptions**, or labelled illustrative if pricing can't be verified; the frame-sampling policy and its risk;
    - prompt screening in production;
    - CSAM hash matching (vetted service, must complete before publish, fail closed while it is down);
    - 1M uploads/day: about 11.6 uploads/s on average, peaks several times higher, and frame expansion for video. Likely first bottleneck is the claim query and per-workspace lock contention, plus the provider's rate limits. Metrics that would confirm it: claim latency p99, lock waits, oldest job age, and provider 429 rate. First change: partition the claim by workspace or shard, or move dispatch to a broker with a transactional outbox, once measurements show the need.
11. **Production plan:**
    - first 15 minutes: stop unchecked publication (fail-closed flag or pause publishing), block generation, check provider health, define the affected window, preserve logs and records, stop duplicate scoring;
    - rollout: canary → staged, with gates on published-unchecked = 0, held rate, backlog age and 5xx;
    - rollback triggers, and never rolling back to a fail-open version;
    - recovery verification: reconcile uploads against results, re-screen held media, watch both workspaces drain, audit reports zero;
    - historical content review kept separate from software recovery.

**AI.md:** a log kept during the work of the tools used, what was verified and how, and real incorrect suggestions caught (for example, the deadlock and attempt-budget flaws found while reviewing Revision 1), plus what remains unverified. **DECISIONS.md:** short ADRs, at least: PostgreSQL queue vs broker, fencing at commit vs cancellation, no SQL policy recompute, outage-held items visible but not approvable plus the `screening_failed` reason, workspace cap trade-off, probe strategy and its cost, no callback inbox.

---

## 22. Phases and Time Budget (6–7 hours)

| Phase | Work | Budget | Stop condition |
|---|---|---:|---|
| P0 | Environment check (Docker, Python, Node), move private files, `git init`, compose, Dockerfiles, lockfiles, naive schema, seed, **complete** fake provider | 0:45 | `docker compose up` healthy; provider `/score`, `/_control`, `/_stats` and the callback work |
| P1 | Naive API, worker and UI with bugs 1–5 | 0:45 | Upload → published in the naive system |
| P2 | Incident tests, Vitest test, workload script, audit A/B; run at the baseline; save evidence; **tag** | 0:45 | All five incident tests fail for the stated reason; evidence saved |
| P3 | `0002_hardening` + SQL tests; policy, oracle and prompt modules + unit tests; fixes 1–5 | 1:30 | Incident and unit tests green |
| P4 | Retry classification, circuit breaker, sweeper, async and callback hardening, fairness cap, metrics and logs | 1:15 | Integration tests green |
| P5 | Workload after (20 and 40 frames), crash test, full suite, fresh-clone check | 0:30 | `evidence/after/` complete |
| P6 | README, AI.md, DECISIONS.md | 0:45 | All README sections filled from evidence |
| **Total** | | **6:15** | WALKTHROUGH.md is interview prep, done after the timed work and reported separately |

**If a phase overruns,** work is cut in this order and each cut is stated in the README:
1. mypy and the CI compose job;
2. the Hypothesis test (the table tests stay);
3. the 40-frame workload variant (`test_async_video_callback` stays);
4. the container `kill -9` script (the in-process fault-injection test stays);
5. frontend action tests (the four state tests stay).

These are never cut:
- any of the 9 rules;
- the incident tests;
- before/after evidence;
- the audit;
- recovery after an outage;
- fencing.

---

## 23. Acceptance Criteria

**Contract and rules**
1. Every required route keeps its exact status codes and response shapes. Provider modes are exactly `ok/slow/timeout/error`, with the `fail` alias.
2. A prohibited label ≥ 0.8 in any frame → `blocked` in every workspace. 0.5 ≤ score < 0.8 → `held(needs_human_review)`.
3. `explicit` ≥ 0.8 is published in verified workspaces and blocked in unverified ones, unless a stricter rule applies.
4. Prohibited generation prompts return `422 {"reasons"}`, and no media row or provider call is created.
5. If no decision can be obtained, the media is `held` and never `published`. The database guard makes publication without a full-coverage `published` result, or an admin-approved `held` result, impossible.
6. Only workspace admins approve or reject. Members get `403`. Outage-held and failed-screening media can't be approved (`409`).
7. Unsigned or invalid callbacks get `401`, with the HMAC computed over the raw bytes and compared in constant time. Valid callbacks are idempotent and never undo terminal states.
8. At most one `screening_results` row per media. A redelivered job doesn't cause a second concurrent provider call, and a stale worker can't overwrite a result.
9. Every frame is screened, and provider responses must cover every frame and all six labels.

**Isolation and auth**
10. Any member of the owning workspace can read its media. Other workspaces get `404`. 401 and 403 are decided before any lookup.

**Reliability**
11. Timeout, connection error, 5xx, 429, 409 and malformed responses are each handled as in Section 6. Circuit-open skips don't consume attempts. 429 has a separate per-cycle limit. Budgets reset per recovery cycle.
12. After recovery, outage-held media is re-screened automatically. The probe uses an existing `held_provider` job and never a fake media ID.
13. A provider that never calls back is detected by the watchdog and recovered without scoring again (fake provider).
14. A 200-video burst doesn't starve the other workspace's images, during the outage or after it. The cap holds with 2 containers × 4 tasks, and its trade-off is documented.
15. The crash-after-scoring test is real and reproducible, and its limits are stated.

**Observability and evidence**
16. Logs let one workspace, media item, job and attempt be followed through failure and recovery. No secrets or prompts are logged.
17. `oldest_unresolved_job_age_s` covers `queued`, `claimed`, `waiting_callback` and `held_provider`. Failed jobs have their own metric and an alert rule.
18. The before/after tables show published unchecked, provider calls per upload and oldest waiting job age for the same workload, all measured.
19. The baseline fails all five incident tests and the fixed version passes them. The output is saved, and the baseline can be reproduced from its tag.
20. The UI distinguishes loading, failed, empty and data states.

**Delivery**
21. A fresh clone runs with `docker compose up` (verified and saved), with lockfiles, tests, README, AI.md, DECISIONS.md and git history including the tag. No secrets or private assignment files are committed.

---

## 24. Remaining Risks

1. **Provider idempotency is provider-specific.** Fencing prevents stale local writes, not remote double billing. Mitigations are listed in Section 18.
2. **No durable callback inbox.** The system relies on provider retries plus the watchdog and re-request. A signed callback that the API gets but fails to commit is retried by the provider. If the provider gives up, the watchdog takes over.
3. **Probe cost.** During long outages, at most one call per cooldown period (backing off to 60 s). It shows up in the calls-per-upload metric.
4. **Static cap.** It isn't work-conserving (Section 9.2).
5. **Keyword prompt screening.** It has false positives and semantic bypasses (Section 14).
6. **Single-host measurements.** They aren't capacity evidence.
7. **Time.** The scope is sized for about 6¼ hours with AI assistance. The cut order in Section 22 applies if it overruns, and actual time is reported honestly.

---

*End of plan. Implementation followed in the commit history, starting at "Scaffold: compose stack, fake provider, naive schema and seed".*
