# Decisions

Each entry is a short record: context, options, choice, consequence.

## ADR-1: PostgreSQL is the queue; Redis is only a hint

- **Context:** A job must never be lost between "media accepted" and "screened", and the job and its media have to change together.
- **Options:**
  - Celery or RabbitMQ: needs an outbox and relay to avoid losing a message after a commit.
  - Redis lists: not durable enough to be the record.
  - A PostgreSQL table with `FOR UPDATE SKIP LOCKED`.
- **Choice:** a PostgreSQL job table. Media and its job are inserted in one transaction. Redis holds a wake-up list, the circuit breaker, the 429 pause and counters.
- **Consequence:**
  - No outbox is needed, and fairness, caps and fencing are SQL that can be tested.
  - Losing Redis loses no work: workers poll every `WORKER_POLL_INTERVAL_S`.
  - The ranked claim query is the scaling limit (README, Part 3).

## ADR-2: Ownership is lease token + live lease, not `status`; fencing at commit, no cancellation

- **Context:** Bug 4. A job put back to `queued` while its first attempt was still running got a second, concurrent provider call.
- **Options:**
  - status-based claims (the bug);
  - advisory locks held across the HTTP call;
  - a lease with a fencing token.
- **Choice:** a lease with a fencing token.
  - A claim needs `lease_expires` to be NULL or in the past.
  - Heartbeats can't revive an expired lease.
  - Every job write is `WHERE lease_token = mine`.
  - In-flight HTTP is not cancelled when ownership is lost; the fenced commit is the guarantee.
- **Consequence:**
  - A stale worker can't overwrite anything.
  - A request that already reached the provider may still be processed there, which the idempotency key mitigates.
  - No database transaction is ever held across an HTTP call.

## ADR-3: The database guards publication but does not contain the policy

- **Context:** "Published without a valid screening" should be impossible, not merely untested.
- **Options:**
  - application checks only;
  - a trigger that re-computes the policy in SQL;
  - a trigger that checks evidence and legal transitions.
- **Choice:** the third. `published` requires a result covering every frame with decision `published`, or a `held` result plus an approval by an admin of that workspace. Terminal states stay terminal. Results and reviews are append-only.
- **Consequence:**
  - There's one copy of the policy (`app/policy.py`), checked by an independent test oracle and the audit.
  - The database trusts the decision the app recorded. That trust boundary is stated in the README.

## ADR-4: Outage holds are visible, can be rejected, and can't be approved; a third hold reason

- **Context:** In the incident, moderators saw "Nothing to review" while items waited, but approving something that was never screened would be fail-open.
- **Choice:**
  - `GET /review` returns every held item with `hold_reason` and `reviewable`. Approve returns `409 not_screened` unless the hold is `needs_human_review`.
  - Reject works on any hold and cancels the job, so a rejected item is never re-screened and paid for.
  - A third hold reason, `screening_failed`, marks non-retryable failures (repeated malformed answers, a 4xx from the provider) that need an operator. It is counted in `failed_jobs` and raises an alert.
- **Consequence:** the review page is never misleadingly empty, and a human can't bypass a missing decision.

## ADR-5: Per-workspace cap on live leases (not work-conserving)

- **Context:** A 200-video burst must not starve another workspace's images, during an outage or after it.
- **Options:**
  - rank interleaving only;
  - a static per-workspace cap;
  - a work-conserving cap (enforced only when others are waiting);
  - weighted fair queuing.
- **Choice:** rank interleaving (A1, B1, A2, B2 …) plus a static cap of 4 per workspace out of 8 tasks.
  - Interleaving decides who goes *next*.
  - The cap keeps capacity free, so B's wait doesn't depend on how long A's calls hang.
  - Cap checks lock the workspace row with `FOR NO KEY UPDATE`, which doesn't block uploads.
- **Consequence:**
  - A workspace bursting alone uses only half the workers.
  - Measured result: B's images reached `held` within 3 s (p95) during the outage while A's burst was queued.
  - **Revisit when:** there are many tenants, or the idle capacity matters. Then switch to a work-conserving or per-share cap.

## ADR-6: Per-cycle retry budgets, a shared circuit breaker, and a probe that re-uses a real held job

- **Context:** Retrying 200 jobs against a dead provider is a retry storm. Recovery must happen without a human, and the plan's first revision could deadlock: every job exhausted, so nothing left to probe with.
- **Choice:**
  - Each job has a 4-failure budget per recovery cycle, with jittered backoff.
  - 429/409 responses have a separate limit and never count as failures.
  - A breaker shared in Redis means an open circuit skips jobs without using an attempt. A half-open circuit allows one canary call, and each failed canary doubles the cooldown, up to 60 s.
  - When the circuit is half-open and no due job exists, the sweeper re-queues the **oldest existing `held_provider` job** as the canary.
  - A closed circuit re-queues held jobs with a fresh cycle.
- **Consequence:**
  - A long outage costs at most one probe call per cooldown period.
  - No synthetic media ID ever appears in `/_stats`.
  - Detecting recovery takes up to one cooldown period (8 s in tests).

## ADR-7: Idempotency-Key = media ID; the fake provider scores on receipt

- **Context:** A worker can die after the provider did the work but before we saved it, and every call costs money.
- **Choice:** the media ID is the idempotency key on every call. The fake provider stores its result per key and replays it, and repeated async requests re-send the stored callback. `/_stats.calls` still counts every request.
- **Consequence:**
  - Recovery after that crash costs one extra request but no extra scoring, *if* the vendor honours keys.
  - The fake provider proves only the pattern.
  - In production, add a durable result handoff and reconcile billing against `job_attempts`.

## ADR-8: Callbacks: HMAC over raw bytes, strict schema, no durable inbox

- **Choice:**
  - Verify the hex HMAC-SHA256 over the exact raw body (optional `sha256=` prefix) with `hmac.compare_digest`, before parsing.
  - Then validate a strict schema: `{media_id, request_id, frames}`.
  - Then lock the media row and insert the single result `ON CONFLICT DO NOTHING`. The callback never changes a decided item.
- **Consequence:**
  - Duplicates and replays are harmless.
  - There's no inbox table and no nonce, because the contract doesn't include those headers.
  - Lost callbacks are covered by provider retries plus the 60 s watchdog and a re-request.

## ADR-9: asyncpg with raw SQL; Alembic runs on psycopg

- **Context:** The important logic is SQL (claim, fencing, guard), and it should read exactly as it runs.
- **Choice:** asyncpg with explicit transactions, and no ORM.
- **Consequence:**
  - Alembic runs on psycopg, because asyncpg's prepared statements can't hold multi-statement migration SQL. This was found when the first migration crashed.
  - Two database drivers in one image, but each has one job.
