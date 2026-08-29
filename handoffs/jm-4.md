**Ticket:** JM-4 — Jeene Mode: the plans API

**Branch:** `jm-4-plans-api` (stacked on `jm-3-resolution-and-progress` → `jm-2-tables-and-fallback-planner` → `jm-1-scope-and-inventory`)

**Summary:**
The whole feature now works server-side with no provider configured. Nine plan endpoints plus the ownership-scoped batch question fetch the app needs to open a step. `app/plans/store.py` is the only module that touches the three plan tables; `app/routers/plans.py` assembles them; `app/questions.py` fetches a known set of questions under the same answer-integrity rules the practice deck uses. Adding a model in JM-5 is a change to one function, `_generate`, and to nothing else.

**Files changed:**
- `app/plans/store.py` (new) — plan CRUD, the two limits, and `step_states()`: every step of many plans with the student's answers against its frozen questions, in one query.
- `app/routers/plans.py` — the nine endpoints, plus JM-1's admin debug view unchanged.
- `app/questions.py` (new) — `fetch_questions_by_ids()`, shared by the batch endpoint and the placement check. Returns questions in the order asked for, because a step's frozen list *is* an order.
- `app/routers/content.py` — `GET /questions?ids=`, scoped to ids the caller's own plans actually froze.
- `app/schemas.py` — `PlanSummary`, `PlanStep`, `PlanStepItem`, `PlanDetail`, `PlanCreate`, `StepItems`, `CheckpointResult`.
- `app/plans/progress.py` — `state_from_counts()` extracted so the single-step and bulk paths share one rule.
- `db/backend_schema.sql` — `study_plans.summary` and `study_plans.subject`, both in the `CREATE TABLE` and as idempotent `ALTER`s so a database that already has JM-2's table lands in the same place.
- `tests/test_plans_api.py` (new) — 32 tests, none of which skip.

**How to test:**
1. `.venv/bin/python -m pytest tests/ -q` → expect `173 passed, 17 skipped`.
2. Apply the DDL (twice — every statement is `IF NOT EXISTS`), then with a real `DATABASE_URL` and a signed-in student:
   ```
   curl -X POST .../plans -H "Authorization: Bearer $TOK" -H 'Content-Type: application/json' \
     -d '{"scope_node_id":"phy_11_ch4","proficiency":"intermediate","intent":"first_time"}'
   ```
   → a plan with 3–6 steps, `origin` invisible to the client but `fallback` in the row.
3. **Repeat the exact same POST.** It must return the *same* `plan_id`, not a second plan.
4. `POST` for three different scopes, then a fourth → `409` naming the stalest plan to archive.
5. `POST .../plans/{id}/steps/{sid}/complete` on a practise step → `422` telling you how many questions would complete it. On the `learn` step → `200`.
6. `POST .../plans/{id}/steps/{sid}/skip` on the checkpoint → `422`.
7. `GET .../plans/{id}` twice — the current step's `question_count` must be identical both times, and `question_ids` must not change.
8. `GET .../questions?ids=<one from your plan>&ids=<one that is not>` → only the first comes back, with no indication the second existed.
9. **Rate limits across workers:** run `uvicorn app.main:app --workers 2`, then POST four plans in a minute → the fourth must be refused whichever worker serves it. This is the acceptance criterion that cannot be checked without a database.

**Acceptance criteria:**
- [x] All nine endpoints exist with typed Pydantic responses and appear correctly in the OpenAPI docs — verified by a test that walks the generated spec and fails any `/plans` operation whose 200/201 body is not a named schema.
- [x] `POST /plans` returns the existing active plan for a scope rather than creating a second — checked before both limits, so resuming is neither a new plan nor a spend. A lost race on the unique index also resumes.
- [x] A fourth active plan is refused with a message naming which to archive — names the one untouched longest, since `active_plans` orders by `updated_at`.
- [x] Self-completing a graded step returns 422 — and the message says how many questions would complete it, rather than just refusing.
- [x] The checkpoint cannot be skipped.
- [x] `GET /questions?ids=` refuses ids not in one of the caller's own plans, and applies the unreleased-test guard — ids outside the caller's plans are silently absent rather than refused, because telling a caller which of their guesses existed is the enumeration they were after.
- [~] Per-account rate limits enforced from the database, **verified across two workers** — the limits are counted in the database (asserted by test: no in-process counter, and the count does not filter on status, so archive-and-retry is not a refund). **The two-worker verification has not been run**, because it needs a database. Step 9 above.

**Decisions taken in this ticket:**
- **Only the current step's questions are frozen when a plan is opened.** Not a performance choice — the checkpoint asks for questions the student has not seen, and freezing it at plan-open time would draw that list *before* they work through steps two to five, filling the checkpoint with questions they are about to meet. A checkpoint has to resolve late or it measures nothing.
- **`planned_count` and `question_count` are separate fields.** The first is what the plan asked for; the second is what exists and is null until the step is frozen. Never estimated from the first, because a step that says ten and opens on six is a bug the student can see.
- **Rate limits count every plan created in the window, whatever became of it.** Counting only active ones would make create-archive-repeat a way around the limit.
- **A scope with nothing published is a 409, not an empty plan.** A plan row with no steps sits in the history looking like work the student failed to do.
- **`GET /questions` returns nothing for an id outside your plans, rather than 403.** A refusal confirms the id exists.

**Defects found and fixed during the audit** (all in code written in this ticket):
- **An N+1 on every plan open, and a history percentage that would have disagreed with it.** The first draft derived each step's state with its own query, and the history screen dodged that by reading `study_plan_steps.state` — which for a graded step is always `pending`. A plan would have read 0% in the history and 60% on the plan screen. Replaced with one bulk query (`store.step_states`) and one shared derivation (`progress.state_from_counts`), used by both.
- **`study_plans` had no `summary` column** and the store wrote to it. Added, with the `ALTER` so a database that already ran JM-2's DDL is not left behind.
- **`subject` was declared on the wire model and never populated.** The history card wears the subject's colours, and a subtopic carries no subject of its own — so it is denormalised onto the plan at save time rather than costing a tree walk per history row.
- **The placement endpoint had `response_model=list`** — untyped, which the OpenAPI acceptance criterion exists to prevent. Now `list[Question]`, and a test walks the whole spec so the next one cannot slip through.
- **A create race would have 500'd instead of resuming.** Two taps on two workers both pass the limit check; the partial unique index then rejects the second insert. Now caught and turned into a resume, which is what the student asked for anyway.
- **A dead progress query** in `step_items`, and one test that asserted on a docstring rather than on behaviour (the same trap as JM-1 — a docstring containing the string it bans).

**Verification actually performed:**
- All 29 SQL statements across the new and changed modules parse under `sqlglot`'s Postgres dialect; all 10 DDL statements parse and every one is `IF NOT EXISTS`.
- The generated OpenAPI spec was walked to confirm every `/plans` operation returns a named schema.
- `pyflakes` clean across `app/`.
- **Not performed: still nothing has been executed against a real Postgres.** Four tickets deep. The endpoints have never returned a row, no plan has ever been written, and the two-worker rate-limit criterion is unverifiable without one. **I would not start JM-5 before running steps 2–9 above.** JM-4 is the first ticket where the code produces something a person can look at, which makes it the natural place to stop and do that.

**Notes for JM-5:**
- `_generate(inventory)` returns `(plan, origin, provider, model)`. The provider goes above the fallback inside that function; nothing else changes.
- `PROMPT_VERSION` lives in `plans.py` and is written to every plan. Bump it when the rubric changes.
- The validator will need `scope.closure_ids` from JM-1 and the `unrated` rule from JM-2 (selectable, but a checkpoint may not rest on it alone).

**Notes for JM-7 to JM-9:**
- Opening a step is one request: `GET /plans/{id}/steps/{sid}/items` returns the items *and* the full questions, with stem and option figures and no answer.
- Video items carry a `player_url` on the backend's own domain — the client should load that, not a YouTube URL. `content.py` records why in detail.
- Notes items carry only `notes_chapter_id` for now; JM-9 adds the viewer page.
- `StepItems` deliberately carries no progress. The client refreshes the plan when it closes the sheet, which is where the updated "4 of 8 right" belongs.
