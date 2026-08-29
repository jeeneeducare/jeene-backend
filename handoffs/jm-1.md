**Ticket:** JM-1 — Jeene Mode: scope resolution and the inventory builder

**Branch:** `jm-1-scope-and-inventory`

**Summary:**
Adds `app/plans/`, the deterministic half of Jeene Mode. No model is called anywhere in this ticket and no plan is produced yet; what ships is the thing a planner will later be given, plus the guarantee about what it will never be given. `scope.py` resolves a chapter, topic or subtopic to its published subtree, closes that over the authored `nodes.prerequisite_node_ids` graph, and derives a bounded list of foundation candidates from keyword overlap with earlier chapters of the same subject. `record.py` derives the student's standing in the scope from the attempt log. `inventory.py` is the boundary: it assembles the catalogue — node facts, video and notes items, per-concept question buckets, the student record — and its SELECT lists are the whole of the content guarantee, naming every column so that nothing widens the payload by accident. `GET /plans/debug/inventory` returns it for an admin, so the catalogue can be read with the teacher before anything consumes it.

Two correctness rules were extracted rather than copied, because a third copy is how this repo has been bitten before: `app/ranking.py` now owns the Wilson ordering (`mistakes.py` re-exports it under its old private names, so its tests are untouched), and `app/visibility.py` now owns the unreleased-test-paper guard (`content.py` re-exports it likewise). Both extractions are behaviour-preserving and covered by the existing suites.

**Files changed:**
- `app/plans/schema.py` (new) — the inventory contract as Pydantic models. Deliberately not in `app/schemas.py`: that file is the app's API contract and none of this is served to the app. Notable: `QuestionBucket` is keyed by node/type/difficulty and carries counts only, so there is no field anywhere in this file that could hold a question id.
- `app/plans/scope.py` (new) — `resolve_scope()`. Recursive CTEs for the subtree and for the walk up to the enclosing chapter; a depth-bounded `UNION` closure over `prerequisite_node_ids` (bounded because that column is free text to Postgres and one typo makes a cycle); the keyword-overlap query for derived candidates. `ResolvedScope.closure_ids` is the set JM-5's validator will check plan node references against.
- `app/plans/record.py` (new) — the student's scope accuracy, coverage and weak concepts, plus `concept_standing()` for foundation candidates. Attribution differs on purpose between the two: coverage counts every concept mapping, because that is what `/concepts/{id}/questions` will actually fetch; weakness counts only the primary mapping, because that is how the Mistake Book attributes and the two screens must not disagree about who is weak.
- `app/plans/inventory.py` (new) — `build_inventory()` and `FORBIDDEN_COLUMNS`. Buckets, materials (videos below the scope, falling back to the nearest ancestor and marked `inherited`; chapter notes; papers overlapping the scope), and the subtopic rollup for oversized chapters.
- `app/routers/plans.py` (new) — `GET /plans/debug/inventory`, admin-only, tenant taken from the admin's own row.
- `app/ranking.py` (new) — `wilson_lower_bound` / `worth_doing`, moved verbatim from `mistakes.py`.
- `app/visibility.py` (new) — `NOT_UNRELEASED_TEST_SQL`, moved verbatim from `content.py`.
- `app/routers/mistakes.py` — imports the ranking helpers and aliases them to their old private names; 39 lines of duplicated statistics removed.
- `app/routers/content.py` — `_NOT_UNRELEASED_TEST` now aliases the shared constant.
- `app/main.py` — registers `plans.router`.
- `tests/test_plans_inventory.py` (new) — 27 tests, none of which skip.

**How to test:**
1. `python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt`
2. `.venv/bin/python -m pytest tests/ -q` → expect `64 passed, 17 skipped` (the 17 are the pre-existing integration tests that need `DATABASE_URL`).
3. `.venv/bin/python -m pytest tests/test_plans_inventory.py -q` → expect `27 passed`, with no skips. The boundary tests must never skip.
4. With a real `DATABASE_URL` and an admin row for your uid: `uvicorn app.main:app --reload`, then
   `curl -H "Authorization: Bearer $ID_TOKEN" "http://127.0.0.1:8000/plans/debug/inventory?node_id=phy_11_ch4"`
5. In that response, confirm by eye: no question text, no options, no `correct_option_ids`, no explanation text, no image or PDF URL, and no question id anywhere. `question_buckets` should hold counts; `scope_question_total` should be **less than** the sum of the bucket totals wherever questions are multi-tagged.
6. `curl ... "?node_id=phy_11_ch4_inertia_first_law_statement"` (a concept) → expect `404` naming the three types a plan can be about.
7. Without an admin row → expect `403`; with no token → expect `401`.

**Acceptance criteria:**
- [x] `resolve_scope()` returns the subtree plus the transitive authored prerequisite closure for a chapter, topic or subtopic — implemented; non-plannable types return `None` (test: `test_only_a_chapter_topic_or_subtopic_can_be_planned`).
- [x] Foundation candidates are derived from earlier same-subject chapters sharing `search_keywords`, capped and ordered — capped at 6 total, authored first and never crowded out, ordered by shared-keyword count then chapter position, and filtered to concepts with at least 3 published questions.
- [x] `build_inventory()` returns exactly the documented contract and reads no forbidden column — with two documented extensions to the contract in the plan document: a `subtopics` list (so every id a rolled-up bucket can carry is resolvable) and `scope_question_total` (see below).
- [x] `test_inventory_never_carries_content` passes and fails when a content field is deliberately added — the check reads every SQL literal in all three modules; `test_the_check_actually_fails_when_a_content_column_is_added` proves the checker itself catches what it names.
- [x] Bucket rollup triggers above the documented threshold and is recorded in the payload — `_MAX_BUCKETS = 200`, rolled to subtopic, and `rollup.applied` reports what actually happened rather than what was attempted.
- [x] An admin-only `GET /plans/debug/inventory?node_id=` returns it — **without** the `as_student` parameter originally sketched; see below.

**Deviations from the plan, and why:**
- **No `as_student` on the debug endpoint.** The sketch had a uid parameter so a reviewer could see a real plan's inputs. That would have turned a content-admin row — today "may attach a video" — into a way to read any student's per-concept performance. That is a different permission and should be argued for on its own rather than acquired as a debugging affordance. The catalogue is the reviewable half; JM-4's real endpoint exercises the student path for the caller themselves.
- **`node_videos.duration_seconds` is not read.** The column does not exist until JM-2's migration. Selecting it now would break any deploy that beat the DDL, so `VideoItem.duration_seconds` is null and the call site is commented with where to start reading it. JM-2's acceptance criteria already cover adding the column.
- **`scope_question_total` added to the contract.** Bucket totals cannot be summed: a question tagged to two concepts sits in two buckets. Without this the planner would believe a scope holds more than it does and would size a checkpoint it cannot fill (test: `test_scope_question_total_is_not_the_sum_of_the_buckets`).
- **`unrated` is a bucket difficulty.** `questions.difficulty` is nullable and plenty of rows have no grade. Hiding them would make a scope look emptier than it is, so they are counted honestly under `unrated`. **JM-5 must decide whether a selector may request it** — the plan's selector enum does not currently include it.
- **Two extractions beyond the ticket.** `ranking.py` and `visibility.py`. Both are correctness rules that JM-1 needed a second reader for, and duplicating either is the exact failure `figures.py` was written to end. Both moves are verbatim and the existing tests pass unchanged.

**Verification actually performed:**
- All 15 SQL constants parse under `sqlglot`'s Postgres dialect.
- Every `table.column` reference cross-checked against `db/backend_schema.sql` and the pipeline's `db/schema.sql`; all resolve.
- Every query asserted parameterised and tenant-scoped by test, not by review.
- **Not performed: no query has been executed against a real Postgres.** No server is available on this machine (`libpq` client only). This is the top risk in the ticket and the first thing to do with `DATABASE_URL` set — see step 4 above.

**Notes for whoever picks up JM-2:**
- `chapter_notes` is queried by `content.py` and by this ticket, but is declared in **neither** checked-in schema file. Worth finding out where it lives before building further on it.
- `_TESTS_SQL` evaluates its `EXISTS` twice per row, in the select list and again in `HAVING`. Harmless at today's handful of papers; revisit if the test bank grows.
- `resolve_scope`'s subtree query duplicates `content.py::_fetch_chapter_subtree` in shape but generalises it to any node type. Folding the two together was left out of this ticket to keep the diff reviewable; it is a clean follow-up.
