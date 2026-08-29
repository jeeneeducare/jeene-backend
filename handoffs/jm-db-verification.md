**Ticket:** JM-1 to JM-4 — verification against a real database

**Branch:** `jm-4-plans-api` (the verification pass sits on top of the four stacked tickets it verifies)

**Summary:**
JM-1 through JM-4 were built without a single query ever being executed — every handoff said so, and each one recommended fixing that before the next. This is that pass. A local Postgres 17 was stood up, both schemas applied, a deterministic seed written, and 27 integration tests added that exercise the endpoints end to end against real rows.

It found five defects, three of them in shipped code and one of them a rule that would have made the feature unusable. It also found that **the app could not connect to a non-TLS database at all**, which is the reason nobody had been able to run any of this locally in the first place.

Every "not verified against a real Postgres" caveat in `jm-1.md` through `jm-4.md` is now discharged, including the two-worker rate-limit criterion that JM-4 had to leave open.

**Defects found, all by running the code:**

1. **The pool could not connect to any database without TLS.** `app/db.py` hardcoded `ssl="require"`. Correct for Supabase and wrong as an absolute: it meant a local Postgres was unreachable, which is why every integration test in the repo was written against Supabase and why four tickets could be written without executing anything. A DSN that carries its own `sslmode` is now believed; absent one, `require` still applies, so production behaviour is unchanged and nothing can quietly downgrade.

2. **An `unrated`-only selector matched every question in the bank.** `[d for d in difficulty if d != "unrated"] or None` collapsed an empty-but-meaningful list into "no filter", and the clause short-circuits on `IS NULL`. A checkpoint asking for ungraded questions would have silently drawn from the whole scope at every difficulty. The same line existed in two modules; both now call one `difficulty_filter()`. **A unit test had asserted the buggy value as correct** — which is why it survived four rounds of review. That test now asserts the opposite and says why.

3. **The two limits made each other unusable.** `MAX_PLANS_PER_HOUR` was 3 and `MAX_ACTIVE_PLANS` is 3, so a student who filled their three slots and archived one could not start the replacement for an hour. Each limit looked reasonable alone; only running the sequence a student would actually perform showed it. Hourly is now 5, and an invariant test keeps it above the active cap.

4. **`chapter_notes` was in production and in no schema file.** `content.py` has queried it since notes shipped, and JM-1's inventory reads it. A database built from version control could not serve `/chapters/{id}/notes` and could not build an inventory. Reconstructed from the queries and added as `CREATE TABLE IF NOT EXISTS`, so production is untouched; if the two have drifted, the live table is the authority and this copy is the one to correct.

5. **The first seed was not repeatable.** `LIMIT 6` with no `ORDER BY`, and attempts inserted with fresh UUIDs, so re-running picked different questions and duplicated rows. Between runs the seeded student's accuracy on the prerequisite drifted from 33% to 75% and a foundation step appeared and disappeared with it. Now deterministic ids and explicit ordering; running the seed three times leaves 17 attempts.

**What was verified, and how:**
- **The content boundary, against real content.** Every seeded question carries distinctive strings in its stem, options, key, worked solution, AI explanation, figures and notes URL. None appears anywhere in what a planner would be sent, and no question id does either. This guard had only ever been checked against source text.
- **Bucket totals really do over-count.** A question tagged to two concepts in the scope makes the buckets sum to one more than the scope holds — which is why `scope_question_total` exists, now measured rather than argued.
- **A question from an unreleased paper is in no count and no deck.**
- **The authored cross-chapter prerequisite is found** — force before gravitation, the case the feature was asked for.
- **Freezing.** A step opened twice returns the same questions, through both the step endpoint and the plan. The checkpoint is *not* frozen before the student reaches it.
- **Counts are never guessed.** An unopened step reports `planned_count` and a null `question_count`; opening it makes both real.
- **Completion is evidence.** Answering a step's questions wrong leaves it `in_progress`; answering the same questions right completes it without anyone marking it, and wrong-then-right counts as right.
- **History and the plan screen agree** on the percentage — the bug JM-4's audit caught, now checked against the database that would have shown it.
- **Every refusal**: graded step not completable, checkpoint not skippable, fourth plan refused by name, another student's plan simply not found, batch fetch returning only ids your own plans froze.
- **The rate limit across processes.** Two separate PIDs with separate connection pools: rows written by one are counted against the other's limit. This is JM-4's outstanding acceptance criterion, and it now passes.
- **Schema idempotence.** Both schema files applied twice; the second run is a no-op.

**Files changed:**
- `app/db.py` — `_ssl_mode()`; TLS still required unless the DSN says otherwise.
- `app/plans/resolve.py` — `difficulty_filter()`, shared with `content.py`.
- `app/routers/content.py` — uses it.
- `app/plans/store.py` — `MAX_PLANS_PER_HOUR` 3 → 5, with the reason recorded.
- `db/backend_schema.sql` — `chapter_notes`.
- `db/testdata/seed_plans.sql` (new) — a deterministic, idempotent content set.
- `tests/test_plans_integration.py` (new) — 27 tests, skipped without `DATABASE_URL`.
- `tests/test_content.py` — the Supabase-seed tests now detect that the seed is absent and skip, so a local `DATABASE_URL` no longer produces seven failures unrelated to your change.
- `tests/test_plans_resolve.py`, `tests/test_plans_api.py` — the test that encoded bug 2, and limits keyed to the constants rather than to literals.
- `README.md` — how to run locally.

**Results:**
```
no DATABASE_URL          179 passed,  44 skipped
local database           212 passed,  11 skipped   (the 11 are Supabase-seed-specific)
integration file alone    27 passed
```

**What is still unverified:**
- **Against Supabase itself.** This ran on Postgres 17 with a synthetic seed. The live database is the authority on `chapter_notes`' real shape, and on whether the eleven Supabase-seed tests still pass — worth one run with the real `DATABASE_URL` before merging.
- **The app's own behaviour.** Nothing here proves the Android or iOS clients can consume these responses; that is JM-6 onward.

**Recommendation:**
Merge JM-1 to JM-4 together with this pass rather than separately. Bugs 2 and 3 were introduced in JM-2 and JM-3 and fixed here, so the intermediate branches contain known-broken behaviour and are not worth landing on their own.
