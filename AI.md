# AI usage

## Tools

- **Claude Code (Claude Opus 5.5)**, running in VS Code on Windows, used for:
  - reviewing and revising my implementation plan (`PLAN.md`)
  - writing the code, tests, scripts and docs
  - running Docker, tests and the workload
- No other code-generation tools were used.

## How work was checked

<!-- Filled in as phases complete. -->

## Incorrect or flawed suggestions caught

Each entry records what happened, who caught it, and how.

1. **Plan, Revision 1: recovery could get stuck forever** (caught in AI review of my draft plan). Outage-held jobs were only re-queued "when the circuit closes". The circuit only closed after a test call, and that call needed a queued job. After a long outage nothing would be queued, so the circuit would never close. Fixed in Revision 2: the sweeper re-queues one existing held job as the probe.
2. **Plan, Revision 1: re-queued jobs had no attempts left** (AI review). Recovery re-used the job row without resetting its attempt count, so a re-queued job would fail again on its first new attempt. Revision 2 adds a per-recovery-cycle budget.
3. **Plan, Revision 1: the backlog metric would read zero during an outage** (AI review). "Oldest waiting job" only counted `queued`/`claimed`, so it would look healthy exactly when outage-held jobs were piling up.
4. **Plan, Revision 1: the access rule contradicted the spec** (AI review). The 404 rule would have hidden media from other members of the same workspace. The spec says any member of the owning workspace can read it.
5. **Plan, Revision 2: crashed workers' jobs would never be retried** (AI consistency pass). The draft said expired leases were "already claimable", but the claim query only selects `queued` jobs. The reaper now moves expired `claimed` jobs back to `queued`.
6. **Frontend test setup** (AI's own mistake, caught by running the tests). The first Vitest run failed the "empty" test because rendered components leaked between tests: React Testing Library only cleans up automatically when Vitest globals are enabled. Explicit `cleanup()` was added. The two genuine bug-5 failures remained.
7. **Baseline redelivery timer** (AI, caught while reasoning about the workload). With test timeouts of 3 s, a 5 s naive redelivery timer would never fire, so the workload could not reproduce "scored 2–3 times". The naive timer was set to 2 s (a too-short visibility timeout), a realistic cause of the incident.
8. **Migrations crashed on first start** (AI's choice, caught by running it). Alembic was first wired to asyncpg through SQLAlchemy. asyncpg uses prepared statements, which can't hold several SQL commands, so the first migration failed. Alembic now uses psycopg; the app keeps asyncpg.
9. **The redelivery incident test conflated two things** (AI's own test bug, caught when the fixed version still failed it). It set provider slow mode to 3000 ms while the test read timeout was also 3 s. The "slow" call therefore timed out, and a correct retry was counted as a duplicate. The plan's own rule (slow delay < read timeout) had been broken. The test now uses 1500 ms and waits for a final decision. Because the test changed after the tag, the updated test is re-run against `broken-baseline` code for the before-evidence.
10. **Prompt filter false positive** (AI, caught by reviewing the rule before writing tests). "Family word + sexual word" would have refused ordinary adult prompts such as "sexy mom". The incest rule now needs two family terms ("brother and sister ... sex"). "sexy mom" is an allowed-prompt test case.
11. **Smaller slips caught by running the tests.** An ambiguous `lease_token` column in a test's SQL join. Tests started before a restarted api container was healthy (the runs now wait for health). Ruff's B008 rule flagged FastAPI's standard `Depends()` idiom and is disabled with a comment.

## Not verified

<!-- Filled in at the end. -->
