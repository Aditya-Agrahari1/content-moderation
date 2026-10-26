# AI usage

## Tools

- **ChatGPT**, used during planning, implementation review and testing:
  - understanding the assignment and drafting the initial implementation plan;
  - discussing design decisions and potential weaknesses in prompt screening;
  - interpreting test failures, workload results, logs and database output;
  - explaining callback security and helping troubleshoot test execution;
  - reviewing documentation and checking consistency between reported results and saved evidence.
- **Claude Code (Claude Opus 5.5)**, running in Antigravity on Windows, used for:
  - reviewing and revising the implementation plan;
  - writing the repository's application code, tests, scripts and documentation;
  - running Docker, tests and workload experiments.
- I reviewed the implementation, ran the demo and checks manually, and identified issues during testing.

## How work was checked

- **Nothing was accepted because it looked right.** Every behaviour claimed in the README was run, and its real output is saved in `evidence/`.
- **Baseline first.** The five incident tests were written against the naive system and *seen to fail* for the stated reason before any fix (at the `broken-baseline` tag). The final test files were re-run against the baseline code, too.
- **Fix by fix.** Each fix commit was followed by the incident tests. The full suite (172 backend + 10 frontend) was run on a fresh database for the after-evidence, and again in a fresh clone of the final commit.
- **Independent oracle.** Policy correctness is cross-checked against `tests/helpers/policy_oracle.py`, which shares no code with `app/policy.py`, with Hypothesis (300 derandomized cases). The audit uses the same oracle on the original input frames.
- **Real failures, not mocks, wherever possible.** Real PostgreSQL triggers, a real Redis, a real `SIGKILL` of the worker containers, the real provider in timeout and error modes. Only 429/409/malformed responses are mocked, because the provider's four required modes can't produce them.
- **Traces read by eye.** Worker logs were read for one media item through an outage (`evidence/after/sample_trace.log`), to confirm the sequence: retry → open circuit → budget exhausted → probe → canary success.
- **External facts.** Pricing was taken from the official AWS Rekognition and Google Vision pricing pages, accessed 2026-10-26. Azure's page didn't show a number, so none is quoted. The self-hosted GPU cost is marked illustrative.

## Incorrect or flawed suggestions caught

Each entry records what happened, who caught it, and how.

1. **Plan, Revision 1: recovery could get stuck forever** (caught in Claude Code's review of my draft plan). Outage-held jobs were only re-queued "when the circuit closes". The circuit only closed after a test call, and that call needed a queued job. After a long outage nothing would be queued, so the circuit would never close. Fixed in Revision 2: the sweeper re-queues one existing held job as the probe.
2. **Plan, Revision 1: re-queued jobs had no attempts left** (Claude Code review). Recovery re-used the job row without resetting its attempt count, so a re-queued job would fail again on its first new attempt. Revision 2 adds a per-recovery-cycle budget.
3. **Plan, Revision 1: the backlog metric would read zero during an outage** (Claude Code review). "Oldest waiting job" only counted `queued`/`claimed`, so it would look healthy exactly when outage-held jobs were piling up.
4. **Plan, Revision 1: the access rule contradicted the spec** (Claude Code review). The 404 rule would have hidden media from other members of the same workspace. The spec says any member of the owning workspace can read it.
5. **Plan, Revision 2: crashed workers' jobs would never be retried** (Claude Code consistency pass). The draft said expired leases were "already claimable", but the claim query only selects `queued` jobs. The reaper now moves expired `claimed` jobs back to `queued`.
6. **Frontend test setup** (Claude Code's own mistake, caught by running the tests). The first Vitest run failed the "empty" test because rendered components leaked between tests: React Testing Library only cleans up automatically when Vitest globals are enabled. Explicit `cleanup()` was added. The two genuine bug-5 failures remained.
7. **Baseline redelivery timer** (Claude Code, caught while reasoning about the workload). With test timeouts of 3 s, a 5 s naive redelivery timer would never fire, so the workload could not reproduce "scored 2–3 times". The naive timer was set to 2 s (a too-short visibility timeout), a realistic cause of the incident.
8. **Migrations crashed on first start** (Claude Code's choice, caught by running it). Alembic was first wired to asyncpg through SQLAlchemy. asyncpg uses prepared statements, which can't hold several SQL commands, so the first migration failed. Alembic now uses psycopg; the app keeps asyncpg.
9. **The redelivery incident test conflated two things** (Claude Code's own test bug, caught when the fixed version still failed it). It set provider slow mode to 3000 ms while the test read timeout was also 3 s. The "slow" call therefore timed out, and a correct retry was counted as a duplicate. The plan's own rule (slow delay < read timeout) had been broken. The test now uses 1500 ms and waits for a final decision. Because the test changed after the tag, the updated test is re-run against `broken-baseline` code for the before-evidence.
10. **Prompt filter false positive** (Claude Code, caught by reviewing the rule before writing tests). "Family word + sexual word" would have refused ordinary adult prompts such as "sexy mom". The incest rule now needs two family terms ("brother and sister ... sex"). "sexy mom" is an allowed-prompt test case.
11. **The review page could go stale** (found by me, running the demo by hand). With the API stopped, the page kept showing "Nothing to review" and had no button to try again. It only fetched once, and Retry appeared only after a *failed* request. The automated tests passed because each one mounts the page fresh. Fix: the page re-checks every 10 s, shows "last checked", and always has a Refresh button. Two tests were added for it, including a fake-timer test where the API dies while the page is open.
12. **A wrong number in Part 3** (written by Claude Code, caught in the final documentation review). The README claimed "10⁵–10⁶ frames per second" at 1M uploads/day. The correct worst case is 60–120 uploads/s × 60 frames, about **3,600–7,200 frames/s**: the original was about 100× too high. It was corrected, and the arithmetic is now shown.
13. **A price stated as verified that wasn't** (Claude Code, same review). The cost table gave celebrity recognition the same $0.001 per-image rate as moderation, as if confirmed. Only the moderation rate was on the fetched page, so the line is now marked *assumed*.
14. **Stale claims after late changes** (same review). The README still said "6 of 8" baseline frontend failures and a 2 h 45 min total time, after two tests and about 2 h more work had been added. The baseline frontend run was repeated with the final 10-test file (8 of 10 fail), and the fresh-clone check and the time were updated.
15. **A demo check that didn't prove anything** (Claude Code, caught by reading its output). The first database "guard" demo printed `UPDATE 0`: it inserted and updated a row in one SQL statement, which can't see its own insert. So it never hit the trigger, and it left a test row behind. It now uses two statements in a rolled-back transaction, and the real trigger error shows.
16. **Environment, not code** (found by me during the demo). An old project's server, restarted automatically by another IDE's agent, was listening on IPv4 `127.0.0.1:8000` while Docker served IPv6. With our API stopped, the browser fell back to the other server and showed its data. It was diagnosed by tracing the port's owning processes, and the README now says the ports must be free.
17. **A contract mismatch from the plan** (Claude Code's design, caught when we discussed how the graders will test). The plan answered `GET /media/{id}` with `403` for an unknown or mismatched identity, decided before any lookup. The contract says "404 for anyone else", so a literal grader check could have failed. That route now returns `404` for anyone who isn't a member of the owning workspace, and `401` only for missing headers. A test covers other-workspace, mismatched, unknown-user and unknown-workspace callers, and the live checklist gained a check (31 / 31).
18. **Smaller slips caught by running the tests.** An ambiguous `lease_token` column in a test's SQL join. Tests started before a restarted api container was healthy (the runs now wait for health). Ruff's B008 rule flagged FastAPI's standard `Depends()` idiom and is disabled with a comment.

## Not verified

- **Real vendors:** whether any real moderation vendor honours idempotency keys, or bills replays. Only the fake provider demonstrates the pattern.
- **Real crashes beyond a killed process:** machine or power loss, a PostgreSQL crash or fsync behaviour, network partitions, clock skew.
- **Redis down under load:** the breaker's in-memory fallback is unit-tested, but Redis was never stopped during a workload.
- **End-to-end 429 / 409 / malformed handling:** tested with a mocked HTTP transport only.
- **CI:** the GitHub Actions workflow has never run (no remote).
- **Scale:** throughput or behaviour beyond one laptop; every timing in the README is from Docker Desktop with test timeouts.
- **Prompt screening quality** beyond the listed test prompts. It is a deterministic first layer and will miss euphemisms and paraphrases.
- **Part 3 cost figures** are list prices, not quotes. Hive, PhotoDNA, Thorn Safer and GPU pricing were not verified.
