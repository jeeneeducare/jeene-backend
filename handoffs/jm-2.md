**Ticket:** JM-2 — Jeene Mode: plan tables and the deterministic planner

**Branch:** `jm-2-tables-and-fallback-planner` (stacked on `jm-1-scope-and-inventory`, which must merge first)

**Summary:**
Adds the three plan tables and the planner that fills them without a model. `db/backend_schema.sql` gains `study_plans`, `study_plan_steps` and `study_plan_step_items`, plus the `node_videos.duration_seconds` column JM-1 deferred. `app/plans/schema.py` gains the second half of the contract — `StudyPlanOut` and the step, item, selector and completion models that both planners must emit, so nothing downstream has to know which one wrote the plan it is holding. `app/plans/guidance.py` holds the authored instructions, keyed by role and intent, which are the part of a step that makes it a study technique rather than a link. `app/plans/fallback.py` turns an `Inventory` into a plan: up to six steps drawn from seven roles, trimmed by priority, ordered pedagogically, always ending in a checkpoint over unseen questions.

No model is called and nothing is persisted yet — `fallback.plan()` is a pure function from `Inventory` to `StudyPlanOut`, which is what makes every rule in it testable against a hand-written catalogue. Persistence and the HTTP surface are JM-4.

**Files changed:**
- `db/backend_schema.sql` — the three tables, two supporting indexes, the partial unique index that makes "plan this scope" resume rather than fork, and `ALTER TABLE node_videos ADD COLUMN IF NOT EXISTS duration_seconds`. Two table-level CHECKs carry rules the application would otherwise have to remember: `graded_steps_state_their_bar` (a non-`self` step must name its required questions and accuracy) and `item_is_a_reference_or_a_selector` (an item is a named reference or a filter, never both and never neither).
- `app/plans/schema.py` — adds `QuestionSelector`, `PlanItem`, `StepCompletion`, `PlanStep`, `StudyPlanOut` and the `StepKind` / `CompletionKind` / `SelectorOrder` / `ItemType` vocabularies. Module docstring updated: it now holds both halves of the contract.
- `app/plans/guidance.py` (new) — `GUIDANCE`, a role × intent matrix of instructions, plus `ROLE_KIND` and `how_to_use()`. Seven roles covering all four step kinds, three intents each, two to four lines each.
- `app/plans/fallback.py` (new) — `plan()`, the seven step builders, and the trim/order/chain assembly.
- `tests/test_plans_fallback.py` (new) — 32 tests, none of which skip.

**How to test:**
1. `.venv/bin/python -m pytest tests/ -q` → expect `100 passed, 17 skipped`.
2. `.venv/bin/python -m pytest tests/test_plans_fallback.py -q` → expect `32 passed`, no skips.
3. Read a plan rather than only its assertions:
   ```
   .venv/bin/python -c "
   import sys; sys.path.insert(0, 'tests')
   from test_plans_fallback import _inventory, _video, _notes
   from app.plans import fallback
   p = fallback.plan(_inventory(videos=[_video()], notes=[_notes()]))
   print(p.summary)
   for s in p.steps: print(s.kind, '|', s.title, '|', s.how_to_use[0])
   "
   ```
4. Apply the DDL against a scratch database and run it **twice** — every new statement is `IF NOT EXISTS`, so the second run must be a no-op.
5. Confirm the partial index bites: two `INSERT`s into `study_plans` with the same `firebase_uid` and `scope_node_id` and `status = 'active'` must fail on the second; setting the first to `archived` must let the second through.

**Acceptance criteria:**
- [x] Migration creates all three tables and the `node_videos.duration_seconds` column; re-running is a no-op — all eight statements are `IF NOT EXISTS` and parse under `sqlglot`'s Postgres dialect. **Not executed against a real Postgres — see below.**
- [x] The active-scope unique index prevents a second active plan for the same scope — `uq_study_plans_active_scope`, partial on `status = 'active'` so an archived plan does not block a fresh one.
- [x] `fallback.plan()` produces 3–6 steps for the four golden inventories, always ending in a checkpoint — rich chapter, questions-only subtopic, no-record student, real prerequisite gap. All four land in range; the checkpoint is mandatory and survives trimming by construction.
- [x] Steps drop out cleanly when their material is absent, and a questions-only scope still yields a usable plan — no video and no notes means no `learn` step rather than an empty one; fewer than five PYQs means no exam-shape step; a scope of only ungraded questions still gets a checkpoint.
- [~] `how_to_use` strings exist for every step-kind × intent combination **and have been read by the teacher** — the first half is done and tested (`test_every_step_kind_has_guidance_for_every_intent`). **The teacher's read has not happened.** The strings are written and in the file so that review can happen; this criterion is not closed until it does.

**Decisions taken in this ticket:**
- **`unrated` questions may be selected, but a checkpoint may not rest on them alone.** JM-1 flagged this as open. They are real and practisable and excluding them makes a small scope look empty, so a selector may ask for them — but a checkpoint claims to measure at a target difficulty and an ungraded question cannot support that claim, so the checkpoint only widens to `unrated` when the graded pool is too thin, and to "any difficulty" only after that. JM-5's validator should enforce the same rule on model output.
- **An empty `difficulty` or `question_types` list means "any".** Otherwise a planner has to enumerate what it found rather than say it does not care. Documented on `QuestionSelector`.
- **Foundation steps require evidence, not availability.** A candidate needs at least five attempts, accuracy under 50%, and at least three questions to practise. A candidate the student has *never touched* is explicitly not a trigger — that is unknown, not weak, and the placement check is the right way to ask about it. A plan that opens with an earlier chapter every time tells every student they are behind.
- **The checkpoint is the only step that excludes seen questions.** Meeting a question again is how revision works; measuring on one is not.

**Defects found and fixed during the audit** (all in code written in this ticket):
- **`_MIN_STEPS = 3` was a promise nothing kept.** A bare scope — a dozen ungraded MCQs, no lecture, no notes — produces two steps, and the constant claimed three. Renamed `_TARGET_MIN_STEPS` and documented as what a scope with material reaches rather than a floor, because padding to three would mean inventing work. The two-step outcome is now a written-down test.
- **Grammar in a student-facing sentence.** One weak concept produced "g with depth *account* for more of your wrong answers". A plan that cannot write English does not read as one worth following.
- **An empty scope reported "0 steps … about 0 minutes of work."** Now says there is nothing published to work from. JM-4 should refuse before this reaches a screen, but the sentence has to be honest in case it does.
- **Two tests were weaker than they looked.** The own-video-over-inherited test used the same `youtube_id` for both, so it could not have failed; it now uses distinct ids with the inherited one listed first. The too-few-PYQs test asserted on a step *title*, which is exactly the wording this file says it will not pin; it now asserts on selector shape.
- **A dead test helper** (`_roles`) removed.

**Verification actually performed:**
- All eight new DDL statements parse under `sqlglot`'s Postgres dialect, and every one is `IF NOT EXISTS`.
- `pyflakes` clean across `app/` and `tests/`.
- A generated plan was printed and read end to end, which is what surfaced three of the five defects above. Worth repeating whenever the guidance changes — the tests deliberately do not pin wording, so wording problems are only visible by looking.
- **Not performed: the DDL has not been executed.** No Postgres server on this machine (`libpq` client only), unchanged from JM-1. The `IF NOT EXISTS` idempotence claim is checked by reading, not by running twice. This remains the top risk across both tickets.

**Notes for JM-4 and JM-5:**
- `PlanStep.depends_on` is a list of **step indices**; the column is `UUID[]`. JM-4 maps one to the other at persist time.
- `fallback.plan()` can legitimately return zero steps for a scope with nothing published. JM-4 must refuse rather than store that — a plan row with no steps is worse than an error.
- The `consolidate` role is real but rarely survives trimming (priority 40, and seven candidates compete for six slots). It appears mostly when a scope has notes but no video. Not a bug, but do not be surprised by its absence.
- `estimated_minutes` on a `learn` step is a guess (15 minutes) until `node_videos.duration_seconds` is backfilled. The column now exists; the admin path needs to start populating it, and `inventory.py` has a marked line where reading it should begin.
