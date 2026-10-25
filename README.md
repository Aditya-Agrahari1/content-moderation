# Content moderation service: incident reproduction and fix

This is a FastAPI + PostgreSQL + Redis service, with a React/TypeScript review page and a separate fake moderation provider. It screens uploaded and AI-generated images and videos so that:
- nothing prohibited reaches anyone;
- nothing explicit reaches an unverified workspace;
- nothing goes live unchecked, even when the provider is slow, down, or answers twice.

The repository first reproduces last Tuesday's incident with a deliberately broken handler (tag **`broken-baseline`**), and then fixes it in separate commits. Every number below comes from a file in [`evidence/`](evidence/).

## Quick start

```sh
docker compose up --build
```

| Service | Address | Notes |
|---|---|---|
| API | http://localhost:8000 | Callers identify themselves with `X-User-Id` / `X-Workspace-Id` headers |
| Fake provider | http://localhost:8001 | `POST /_control`, `GET /_stats` |
| Review page | http://localhost:3000 | Pick an identity from the dropdown |

The seed creates `ws_verified` (verified) with users `admin_v` and `member_v`, and `ws_unverified` (not verified) with users `admin_u` and `member_u`. The identity headers are a **test-harness mechanism, not authentication**.

```sh
curl -s -X POST localhost:8000/media -H 'X-User-Id: member_u' -H 'X-Workspace-Id: ws_unverified' \
  -H 'Content-Type: application/json' \
  -d '{"type":"video","source":"upload","frames":[{"explicit":0.02},{"explicit":0.95}]}'
curl -s localhost:8000/media/<id> -H 'X-User-Id: member_u' -H 'X-Workspace-Id: ws_unverified'   # -> blocked
curl -s -X POST localhost:8001/_control -d '{"mode":"timeout"}'   # ok | slow | timeout | error (alias: fail)
```

### Tests, workload and crash test

These use the test overrides, which set short timeouts so failures show up quickly. `make` is optional; every command is shown in full.

```sh
docker compose -f docker-compose.yml -f docker-compose.test.yml up -d --build
docker compose exec api pytest                        # 171 backend tests (unit, SQL guard, integration, incident)
cd frontend && npm ci && npx vitest run && cd ..      # 8 review-page tests
docker compose exec api python scripts/workload.py --out evidence/after/workload.json [--video-frames 40]
docker compose exec api python scripts/audit.py       # published-without-screening audit over the whole database
python scripts/crash_test.py                          # host: real SIGKILL of the workers after the provider scored
sh scripts/trace_outage.sh                            # one media item through an outage, grepped across all logs
```

**Reproducing a baseline bug.** The whole incident suite fails on the baseline:

```sh
docker compose down -v
git worktree add ../baseline broken-baseline && cd ../baseline
docker compose -p baseline -f docker-compose.yml -f docker-compose.test.yml up -d --build
docker compose -p baseline exec api pytest tests/incident -v
```

## 1. The incident: failure → cause → evidence

| Symptom last Tuesday | Cause in the handler (marked `# BUG n` at the tag) | Baseline evidence |
|---|---|---|
| Uploads went live unchecked during the outage | **1.** A provider timeout was treated as a pass | `test_timeout_does_not_publish` FAILED: the media was `published` with 0 provider scorings |
| An explicit video went live in an unverified workspace | **2.** Only `frames[:8]` were screened | `test_late_frame_is_screened` FAILED: explicit frame 19 → `published` |
| A prompt for a 16-year-old reached the generator | **3.** Prompts were never checked | `test_prohibited_prompt_refused` FAILED: `202` instead of `422`; workload accepted 5 of 5 prohibited prompts |
| Uploads scored 2–3 times on the invoice | **4.** A job was "owned" by its `status` alone; a 2 s stuck-job timer re-queued running jobs | `test_redelivery_while_running` FAILED: **3 provider calls** for one image |
| "Nothing to review" while 40 items waited | **5.** The page turned a failed request into `[]` | Vitest: an HTTP 500 and a network error both render "Nothing to review" |

**How these combine into the outage story.** When the provider timed out:
1. timeouts published content (bug 1);
2. while calls hung, the short redelivery timer started second and third calls (bug 4);
3. the review page hid its own errors (bug 5).

Bugs 2 and 3 were independent gaps that the outage exposed.

**Baseline evidence files:**
- [`evidence/before/incident_tests.txt`](evidence/before/incident_tests.txt) and [`frontend_tests.txt`](evidence/before/frontend_tests.txt): captured at the tag.
- [`evidence/before/incident_tests_final.txt`](evidence/before/incident_tests_final.txt) and [`frontend_tests_final.txt`](evidence/before/frontend_tests_final.txt): the *final* test files re-run against the baseline application code, because one incident test was corrected after the tag (see [AI.md](AI.md) #9). Result: 4 of 4 incident tests fail, and 6 of 8 frontend tests fail. Four of those six are bug-5 state tests; two test features added in the fix.

**A finished job is not proof of screening.** The incident tests and the audit look at media status and the provider's own evidence (`/_stats`, `screening_results`). They never look at job status.

## 2. The fix

```
POST /media ──(prompt screen, strict validation)──▶ media(pending) + screening_jobs(queued)  [one transaction]
                                                     │   Redis LPUSH = wake-up hint only
worker ×2 containers × 4 tasks ──claim (fair, capped, SKIP LOCKED, lease)──▶ POST provider /score
   │  Idempotency-Key = media id; at most one call per claim; every write fenced by lease_token
   ├─ 200 valid → screening_results (1 per media) + policy → published | held(needs_human_review) | blocked
   ├─ 202       → waiting_callback ──▶ signed POST /provider/callback (HMAC over raw body) → same as 200
   └─ failure   → media held(provider_unavailable) at once; retry within budget; circuit breaker
sweeper (one leader): lease reaper · callback watchdog · probe (re-queue one held job) · recovery re-queue
PostgreSQL guard trigger: no 'published' without a full-coverage result, or an admin-approved held result
```

**Fail closed by construction.**
- `app/policy.py` is the only place thresholds live: a single pure function over **every** frame.
- A provider answer only counts as a decision when it scores every frame with all six labels. Anything else is `malformed`.
- Migration `0002` adds a trigger that **refuses** `published` unless a stored result covers every frame and decided `published`, or a `held` result was approved by an admin of that workspace. Terminal states stay terminal. Results and reviews are append-only.
- **Trust boundary:** the database does not re-run the policy. It checks that complete evidence exists and that state changes are legal. Policy correctness rests on the table tests, a Hypothesis property test against an independent oracle, and the audit's oracle check.

**No duplicate work (bug 4).**
- A worker owns a job by `lease_token` plus a live lease, not by `status`. A row put back to `queued` while its lease is live cannot be claimed.
- Every write checks the token, so a worker whose lease expired and was re-claimed has its result discarded (`stale_discarded`).
- Each media item has at most one `screening_results` row (primary key). Callbacks are idempotent.

**Keeping the queue moving.**
- Each provider outcome is classified separately (table below).
- A per-cycle retry budget uses jittered backoff.
- A Redis-shared circuit breaker stops retry storms. An open circuit skips the job **without using an attempt**.
- The sweeper re-screens outage-held media when the provider recovers. In a long outage where every job is exhausted, it re-queues **one existing held job** as the probe, never a fake media ID.

**Review.**
- `GET /review` lists every held item with `hold_reason` and `reviewable`, so outage-held items are visible and never hidden behind "Nothing to review".
- Approve only works for `needs_human_review` holds: never-screened items get `409 not_screened`.
- Reject works on any hold and cancels the job.

**Isolation.**
- Every media and review query is filtered by the caller's workspace, so other workspaces get `404`.
- Missing headers return `401` and a wrong identity returns `403`, both decided *before* any lookup.
- Composite foreign keys tie users, jobs and reviews to their workspace.

### Retry policy (per recovery cycle)

| Provider outcome | Counts toward the 4-failure cycle budget | What happens | Circuit breaker |
|---|---|---|---|
| 200, all frames and labels | — | Store the result, apply the policy | success |
| 202 (async, > 30 frames) | — | `waiting_callback`; watchdog after 60 s → `held_provider` | success |
| Timeout / connection error / 5xx | yes | Media held; backoff `rand(0, min(8, 1·2ⁿ⁻¹))` s | failure |
| Malformed or incomplete | yes; 3 in a lifetime → job `failed` (ERROR log, `alert=true`, `failed_jobs` metric) | Same | failure |
| 429 | no; separate limit of 10 per cycle | All workers pause for `Retry-After` (default 10 s, max 60 s) | not counted |
| 409 in progress | no; same limit as 429 | Retry after 1 s | not counted |
| Other 4xx (e.g. idempotency conflict) | — | Job `failed`, media `held(screening_failed)`, alert | not counted |
| Circuit open | **no call, no attempt** | Re-try when the cooldown ends | — |

**After a budget runs out:** `held_provider`, with the media held. On recovery the sweeper starts a new cycle with a fresh budget. There's no limit on the number of cycles, because a 40-minute outage has to be survivable.

**Breaker settings:** it opens after 5 consecutive failures. The cooldown starts at 10 s and doubles per failed canary, up to 60 s. Half-open allows exactly one canary call. (Test values: 3 failures, 2–8 s.)

**Fairness.**
- The claim query ranks due jobs inside each workspace and takes rank 1 of every workspace first (A1, B1, A2, B2, …).
- Each workspace may hold at most `MAX_CONCURRENT_PER_WORKSPACE` live leases (4 of 8 tasks by default; 2 in tests).
- Without the cap, A's slow calls could fill every task and B would wait for one to finish.
- **Trade-off:** the cap isn't work-conserving. A workspace bursting alone uses only half the workers. See [DECISIONS.md](DECISIONS.md).

## 3. Before and after: same workload, measured

The workload is `scripts/workload.py`, run with the same seed and parameters against both versions under `docker-compose.test.yml` (3 s provider timeout):
- **A (`ws_verified`):** 200 × 20-frame videos at the start of each phase.
- **B (`ws_unverified`):** one image per second throughout, plus 5 incident videos (explicit last frame), 5 prohibited and 5 safe prompts.
- **Phases:** normal for 20 s, provider `timeout` for 60 s, then recovery until everything is decided.

Raw data: [`evidence/before/workload.json`](evidence/before/workload.json), [`evidence/after/workload.json`](evidence/after/workload.json). The 40-frame async variant is in `workload_40.json` and is consistent with these results.

| Metric (20-frame run) | Before (`broken-baseline`) | After |
|---|---:|---:|
| Uploads accepted | 510 | 508 |
| **Media published unchecked** (audit A ∪ B) | **126**: 87 never scored, 47 against the policy | **0** (A 0, B 0, C 0) |
| Prohibited prompts accepted | 5 / 5 | 0 / 5 (safe prompts refused: 0) |
| Final decisions that disagree with the oracle | 47 | 0 |
| **Provider calls per upload** | **1.159** | **1.024** |
| Provider scorings per upload | 0.829 (timeouts published without scoring) | 1.000 (never scored twice) |
| Max calls for one media item | 2 | 3 (outage retries plus a probe) |
| **Oldest unresolved job, during the outage** (A / B) | 60.3 s / 59.4 s | 61.3 s / 61.1 s |
| Oldest unresolved job, during recovery (A / B) | 62.8 s / 62.5 s | 70.7 s / 68.1 s |
| Backlog drained after the provider recovered | 3.1 s | 11.1 s |
| B images: time to leave `pending` during the outage (p50 / p95) | 41.7 s / 63.5 s (left as `published`!) | **0.9 s / 3.0 s** (`held`) |
| A videos: time to leave `pending` during the outage (p95) | 63.7 s | 8.2 s |

**How to read this table:**
- **The baseline's quick drain isn't a good sign.** It "finished" the backlog by publishing it. After the fix, the backlog is held and then genuinely screened: the drain takes about 11 s, which includes up to 8 s for the breaker to notice the provider is back.
- **"Oldest unresolved job" tracks the outage in both versions** (about 60 s for a 60 s outage). The metric counts every unresolved state (`queued`, `claimed`, `waiting_callback`, `held_provider`), so it does not hide outage-held jobs.
- **The burst doesn't starve the other workspace.** During the outage, B's images reach `held` in 3 s (p95) while A's 200 videos are in the queue. After recovery, B's new images are decided within 7.8 s (p95), mostly breaker recovery lag.
- **Calls per upload fell, even with retries**, because the circuit breaker skips calls during the outage and nothing is redelivered while a call is running. Every failed call is counted honestly in `/_stats.calls`.
- **Scope of these numbers:** one laptop, Docker Desktop, test timeouts, a 60-second outage. They show the behaviour; they are not capacity numbers.

**Other after-evidence:**
- [`after/full_suite.txt`](evidence/after/full_suite.txt): 171 passed.
- [`after/incident_tests.txt`](evidence/after/incident_tests.txt): 4 of 4 pass.
- [`after/frontend_tests.txt`](evidence/after/frontend_tests.txt): 8 of 8 pass.
- [`after/audit.txt`](evidence/after/audit.txt): 923 published items in the whole database, 0 violations.
- [`after/sample_trace.log`](evidence/after/sample_trace.log): one item through the whole recovery path: 5xx retries → open circuit (skip) → budget exhausted → probe re-queue (cycle 2) → doubled cooldown → canary success → published.

**Logs** are one JSON event per line, with `workspace_id`, `media_id`, `job_id`, `attempt`, `cycle`, `lease_token`, `provider_outcome`, `decision`, `from_status`/`to_status` and `latency_ms`. `docker compose logs | grep <media_id>` shows an item's whole history. Prompts, signatures and secrets are never logged; a refused prompt is logged as a SHA-256 hash. `GET /metrics` shows the job backlog by state, the oldest unresolved job per workspace, failed jobs (alert when above 0), held media by reason, provider outcomes, circuit state and counters.

## 4. Worker crash after the provider scored, before the result was saved

**Real crash** ([`scripts/crash_test.py`](scripts/crash_test.py), output in [`evidence/after/crash_test.txt`](evidence/after/crash_test.txt)):
1. The provider is put in slow mode. It scores on receipt, so the "billable" work is done while the worker waits for the response.
2. The script waits until `/_stats.scored == 1`, then sends `SIGKILL` to every worker container.
3. Result: the media is still `pending` with 0 result rows.
4. The workers restart. The lease expires, the sweeper re-queues the job, and it is re-claimed. The second call carries the same `Idempotency-Key`, and the provider answers from its stored result.
5. Outcome: `published` 5.2 s after the crash, **calls = 2, scorings = 1, exactly 1 result row**. `job_attempts` reads `1: never finished, 2: success`.

The same window is also tested in-process, by fault injection at exactly that point (`test_crash_after_scoring_before_saving`).

**What this proves:**
- A worker that dies at that moment publishes nothing.
- The job always recovers, through lease expiry plus the reaper.
- When the provider honours idempotency keys, recovery costs one extra request but no second scoring.

**What it does not prove:**
- Behaviour on machine or power loss, or a PostgreSQL crash and fsync behaviour (only the worker process was killed).
- Network partitions, or clock skew between workers. Lease expiry uses the database clock, which helps.
- That a real vendor supports idempotency keys, how long it keeps them, or whether it bills a replayed request. Our fake provider demonstrates the pattern; it proves nothing about a vendor.

**Closing the gap in production:**
1. Use vendor idempotency keys where they're offered.
2. Use a durable result handoff: the provider, or an edge service, writes results to a durable inbox or queue before we process them, so a crash can't lose a paid result.
3. Run a daily reconciliation of the vendor's billing export against `job_attempts`, to find calls we paid for but didn't record.

A database lock alone can't settle this, because the uncertainty is on the provider's side of the network.

## 5. Remaining limits (what is not done or not proven)

- **Prompt screening is a keyword-and-normalization first layer.** It has false positives (it conservatively refuses any mention of minors) and misses euphemisms and indirect references. See Part 3.
- **No durable callback inbox, no replay nonce.** The callback contract has no timestamp or nonce header. A replayed valid body can only repeat a decision that already exists, because callbacks are idempotent and never undo a decision. Provider retries plus the watchdog cover lost callbacks.
- **The workspace cap is static and not work-conserving** (DECISIONS.md, ADR-5).
- **If Redis is down**, each worker keeps its own breaker in memory, so there are more probe calls (still fail-closed). This fallback is unit-tested but wasn't exercised by stopping Redis under load.
- **429 and malformed responses** can't be produced by the four required provider modes. They're tested at the HTTP-client level with a mocked transport, not end to end.
- **Historical data:** media published unchecked *before* the fix stays published. Migration `0002` doesn't rewrite history; the audit finds those items for a separate content review.
- **Not done or not verified:**
  - mypy;
  - [`.github/workflows/ci.yml`](.github/workflows/ci.yml) is written (lint, unit, frontend, compose integration) but has **never run**, because the repo has no GitHub remote;
  - load beyond one laptop;
  - a frontend test for the reject flow.

## 6. Part 3: cost and the next limit

### Paid API, self-hosted, or a mix?

**A mix.** No single generic API covers all five prohibited categories with the precision these rules need.
- **Paid APIs cover the generic classes well.** AWS Rekognition Content Moderation and Google Vision SafeSearch handle explicit nudity, sexual activity, violence and self-harm.
- **They are weak or missing on the categories that matter most here.** That includes *minor in a sexual context*, *bestiality* and *sexual violence*, and *real person without consent*, which is an identity question, not a visual-content one.

**Covering every category:**
- **explicit, self_harm:** a paid API, which is cheap and good.
- **minor:** a self-hosted age-estimation model on detected faces and bodies, plus CSAM hash matching (below).
- **sexual_violence and bestiality:** self-hosted fine-tuned classifiers, or a specialist vendor such as Hive, whose pricing is not public and must be quoted.
- **real_person:** face matching against a celebrity/public-figure index (Rekognition `RecognizeCelebrities`) **and** against the platform's own consent registry of verified performers.

Any disagreement in the 0.5–0.8 band goes to human review. Explicit content is only a *workspace* rule (verified versus unverified); the five prohibited labels block everywhere.

**Frames per video.**
- **In this exercise:** every frame (1 fps, so 60 per minute).
- **In production:** score every 1-fps frame on the self-hosted prohibited-class models, which is cheap on our own GPUs, plus scene-change keyframes. The paid API runs at the same 1 fps.
- **Why not sample sparser:** a sparser sample (say every 5 s) cuts cost by 5×, but can miss a short prohibited segment, which is exactly what bug 2 did. For always-prohibited classes I wouldn't accept that risk. Sampling, where used, has to be described as partial coverage.

**Estimate for 10,000 images and 1,000 one-minute videos a month.** List prices are from official pages accessed 2026-10-26. These are estimates to verify against a quote. Retries add a few percent: the workload measured 1.02 calls per upload including an outage.

| Component | Basis | Monthly |
|---|---|---:|
| Rekognition image moderation, images | 10,000 × $0.001 ([pricing](https://aws.amazon.com/rekognition/pricing/), first 1M images) | $10 |
| Rekognition moderation, video frames as images | 60,000 frames × $0.001 | $60 |
| *Alternative: Rekognition stored-video moderation* | 1,000 min × $0.10/min (same page) | *$100* |
| Celebrity recognition, images and frames | 70,000 × $0.001 (Group 2 rate) | $70 |
| Self-hosted minor, bestiality and sexual-violence models | About 70k inferences, roughly 1 GPU-hour of compute; an always-on L4/T4 instance is ~$0.5–0.8/h, or $400–600/month always-on, much less if batch or serverless (illustrative, not quoted) | $50–600 |
| CSAM hash matching | PhotoDNA / Thorn Safer: no-cost or contract for qualifying platforms; quote needed | — |
| Google SafeSearch, for comparison | 70,000 units × $1.50 per 1,000 ([pricing](https://cloud.google.com/vision/pricing), units 1k–5M) | $105 |

**Total:** about **$190–750/month**, depending mainly on whether the GPU runs always-on. At this volume the paid API spend is small (under $150), and the human-review time for the 0.5–0.8 band likely costs more than the classifiers.

### Generation prompts in production

The defence is layered:
1. **Normalize**, as done here (NFKC, homoglyphs, zero-width characters, leetspeak, spaced-out letters, ages parsed before folding).
2. **Fast deterministic rules** for unambiguous cases.
3. **A semantic classifier.** This means an LLM safety model or fine-tuned classifier over the normalized prompt and the conversation history, with categories that match these rules, plus embedding similarity against a growing set of known-bad prompts.
4. **Refuse with a generic message**, not the matched rule, so the filter can't easily be probed.

Prompt checks are never the last line of defence. **Generated output is screened exactly like uploads**: the same policy, every frame.

**Handling people who try to get around it:**
- Log the prompt hash and rule.
- Rate-limit and escalate accounts that keep triggering refusals.
- Send borderline cases to trust-and-safety review.
- Red-team the classifier with an adversarial test suite on every model change.
- Track the refusal rate and the escape rate, meaning prompts that passed but whose output was then blocked.

Keyword lists alone fail because adversaries invent new euphemisms faster than any list grows, and because context ("a minor chord", "16 oz") defeats naive matching. The tests here show both cases.

### CSAM hash matching

- **Service:** integrate a vetted service, such as Microsoft PhotoDNA (via NCMEC/Tech Coalition access) or Thorn Safer, which have perceptual-hash lists from NCMEC and other hotlines. Build it as **another mandatory check in the same job pipeline**, before or alongside classification. Its result is stored like `screening_results`, and the publication guard requires a negative hash result as well.
- **On a match:** block immediately; preserve evidence in restricted, access-logged storage; report to NCMEC (a legal duty in the US); suspend the account; escalate to legal and trust-and-safety. Never let a human reviewer "approve" a match.
- **Privacy:** send hashes, not images, where the service allows it. Retain only what the law requires.
- **While the service is down:** fail closed. Media stays `held(provider_unavailable)`, and nothing is published until the hash check has succeeded. The same retry, circuit-breaker and auto-re-screen machinery applies, plus a paging alert, because an outage blocks all publishing.
- **Testing:** never test with real illegal material. Use the vendors' test hashes.

### At 1 million uploads a day, what breaks first?

**Load:** 1M a day averages about 11.6 uploads per second; assume peaks of 5–10×, so roughly 60–120 per second. With videos at 60 frames each, that's 10⁵–10⁶ frames per second into the classifiers at peak.

**What breaks first, in order:**
1. **Provider throughput and rate limits.** At that volume the paid API's per-second quota and our GPU capacity bind before the database does. You'd see a rising 429 rate, the circuit breaker opening, and the oldest unresolved job growing.
2. **The PostgreSQL claim query.** It ranks every due job on every claim (`ROW_NUMBER` over the whole queue) and locks a workspace row per claim. Confirm with `pg_stat_statements` on the claim SQL, claim latency p99, lock waits, and the queue depth trend.
3. **Write volume on `job_attempts` and the JSONB results.**

**What I'd change first:** measure, then move dispatch off the ranked SQL claim. Either partition the queue by workspace with a per-workspace cursor, or dispatch through a broker (Redis Streams, SQS or Kafka) fed by a transactional outbox, keeping PostgreSQL as the record. Batch frames per provider request, and autoscale workers on the oldest-unresolved-job metric. The trade-off is another moving part and an outbox relay to monitor. I wouldn't make that change without numbers showing the claim query is actually the limit.

## 7. Production plan

**First 15 minutes**
1. **Stop the bleeding:**
   - turn on a fail-closed switch (hold everything, publish nothing automatically);
   - pause generation;
   - announce to moderators that the review page may be unreliable.
2. **Check the provider:** look at its status page, our provider error and timeout rates and 429s, the circuit state, and the oldest unresolved job.
3. **Find the window:** media created or published since the first provider error, per workspace.
4. **Preserve evidence:** take a database snapshot and export logs. Don't run cleanup jobs.
5. **Stop duplicate spend:** cap worker concurrency; check provider calls per media.
6. **Assign roles:** an incident owner and a communications owner.

**Roll out the fix**
1. **Migration:** `0002` is additive. Existing published rows are left alone; the guard only checks new transitions.
2. **Gates:** deploy behind the regression gates (incident tests, SQL guard tests, audit = 0 on staging).
3. **Order:** workers first, then the API.
4. **Canary:** one worker container, then 25%, 50%, 100%, with 15–30 minutes per step.
5. **Watch:** published-unchecked (the audit, run every 5 minutes, must be 0), held rate, oldest unresolved job, provider calls per upload, 5xx, and the review-page error rate.

**When to roll back**
- **Triggers:** any unchecked publication found by the audit, a sustained rise in 5xx or `worker_error`, or the oldest job exceeding its SLO. Don't roll back for a held rate that is higher but explained by a provider outage.
- **Never roll back to a fail-open version.** If the new worker misbehaves, keep the publication guard and the fail-closed switch, and roll back only the component causing trouble.

**Verify recovery**
1. Reconcile every upload in the window against `screening_results`.
2. Run the audit over the window: it must report 0.
3. Re-screen media held because of the outage; the sweeper does this automatically, so confirm it with metrics.
4. Check each workspace's backlog drains to 0.
5. Reconcile provider calls per media against the invoice.
6. **Separately**, run a trust-and-safety review of everything published unchecked during the incident, using the audit's list. That's content remediation, not software recovery.

## 8. Time spent and AI use

- **Time:** about 2 h 45 min of implementation, from 22:10 to 00:55 IST.
  - That includes about 45 minutes waiting on slow Docker Hub base-image downloads at setup.
  - Writing and reviewing the plan beforehand isn't included; `PLAN.md` is in the repo.
- **Fresh-clone check:** [`evidence/fresh_clone.txt`](evidence/fresh_clone.txt). A clean `git clone` plus `docker compose up -d --build` with the default compose file started all 7 containers. The contract smoke checks passed, the review page returned 200, and the full suite in that clone passed (171).
- **AI use:** see [AI.md](AI.md) for tools, what was checked, the incorrect suggestions that were caught, and what isn't verified.
- **Design decisions:** [DECISIONS.md](DECISIONS.md).
- **Plan versus implementation:** [PLAN.md](PLAN.md) was the plan. Deviations:
  - the app uses asyncpg with raw SQL rather than an ORM;
  - Alembic runs on psycopg;
  - there are no `models.py` files;
  - `docs/ARCHITECTURE.md` and `IMPLEMENTATION_STATUS.md` were folded into this README;
  - the fake provider never returns 409 itself, because it scores on receipt, so 409 handling is client-tested only.
