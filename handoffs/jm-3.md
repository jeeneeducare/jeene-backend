**Ticket:** JM-3 — Jeene Mode: selector resolution, freezing and completion

**Branch:** `jm-3-resolution-and-progress` (stacked on `jm-2-tables-and-fallback-planner`, which is stacked on `jm-1-scope-and-inventory`)

**Summary:**
Adds the two modules that stand between a plan and the student's actual work. `app/plans/resolve.py` turns a stored selector into real question ids and freezes them onto the item the first time a step is opened, so the deck never reshuffles underneath a student. `app/plans/progress.py` decides whether a step is done — from the attempt log for anything gradeable, from the student's tap only where there is genuinely nothing to measure — and rolls that up into the plan's percentage. `GET /concepts/{id}/questions` gains `difficulty` and `exclude_seen`, which is what the app will need in JM-9 to open a plan step's deck.

Still no model, still no HTTP surface for plans beyond JM-1's debug view. Persistence and the endpoints are JM-4.

**Files changed:**
- `app/plans/resolve.py` (new) — `resolve_selector()`, `freeze_item()`, `selector_from_row()`. Node ids are expanded through a recursive CTE to their concept descendants, so a selector written against a subtopic (which is what a rolled-up inventory produces) resolves the same as one written against concepts. `unrated` is translated to an `IS NULL` rather than compared as a string. The freeze is a conditional `UPDATE ... WHERE resolved_at IS NULL RETURNING`, and a caller that loses the race reads the winner's list rather than its own.
- `app/plans/progress.py` (new) — `step_progress()`, `plan_percent()`, `plan_is_complete()`. Latest attempt per question throughout. A graded step needs both bars — enough answered *and* enough of those right — because either alone is gameable.
- `app/routers/content.py` — `_paginated_questions_for_node_ids` gains `difficulty` and `exclude_seen_for`, both defaulting to the old behaviour so the chapter-questions caller is untouched. The new filter clause is built once and used by both the count and the page query, because a total that disagrees with the rows underneath it is worse than no total. `list_concept_questions` exposes the two as query parameters, validates the difficulty vocabulary with a 422, and takes `optional_user` so `exclude_seen` is simply nothing to apply when signed out rather than an error.
- `tests/test_plans_resolve.py` (new) — 41 tests, none of which skip.

**How to test:**
1. `.venv/bin/python -m pytest tests/ -q` → expect `141 passed, 17 skipped`.
2. `.venv/bin/python -m pytest tests/test_plans_resolve.py -q` → expect `41 passed`.
3. With a real `DATABASE_URL`, confirm the widened endpoint has not changed what it did:
   `curl ".../concepts/phy_11_ch4_inertia_first_law_statement/questions"` → identical to before this branch.
4. Then the new filters:
   `curl ".../concepts/<id>/questions?difficulty=easy&difficulty=medium"` → only those, and `total` must match the number of items across pages.
   `curl ".../concepts/<id>/questions?difficulty=banana"` → `422` naming the four it accepts.
   `curl -H "Authorization: Bearer $ID_TOKEN" ".../concepts/<id>/questions?exclude_seen=true"` → excludes everything in your attempt log; the same call without the token returns the unfiltered set rather than an error.
5. **The freeze, which is the thing most worth checking against a real database.** Insert a plan, a step and a `questions` item by hand, call `freeze_item` twice, and confirm the second call returns the first call's ids and writes nothing.

**Acceptance criteria:**
- [x] A selector resolves once and is stable thereafter; a second call returns the same ids — a frozen item short-circuits before touching the database at all; an unfrozen one writes conditionally and a lost race returns the winner's list.
- [x] `exclude_seen` excludes questions with any prior attempt, and falls back to oldest-attempted when unseen runs out — done as an ordering rather than a filter: unseen first, then longest-ago. A checkpoint that asked for ten in a scope with six unseen still hands over ten, and the four it tops up with are the ones most likely to have been forgotten.
- [x] Under-supply clamps to what exists, never below three, and the shortfall is recorded — the clamp is the `LIMIT`; the shortfall is `sel_count` minus the length of the frozen array, so it is already in the row and there is no third column to keep in step. Logged at INFO. The "never below three" floor lives in `fallback.py` where the count is chosen; the resolver returns what exists, because inventing questions is not an option.
- [x] Completion uses the latest attempt per question; wrong-then-right counts as right.
- [x] Plan percentage excludes skipped steps — and a plan of nothing but skips is explicitly not complete.
- [x] `GET /concepts/{id}/questions` accepts `difficulty` and `exclude_seen` without changing existing behaviour.

**Decisions taken in this ticket:**
- **`study_plan_steps.state` is not the source of truth for a graded step.** It holds `pending` or `skipped`; the real answer is derived on every read. No cache to go stale, no second place to be wrong. If it ever gets slow the answer is a rollup table, not a column written at answer time. `progress.py`'s docstring says this, because it reads as an inconsistency until you know why.
- **The `mixed` ordering is a salted hash, not `random()`.** Two properties were wanted at once: repeatable for a given step, and not identical across students. `md5(item_id || question_id)` gives both. Unsalted — which is how it was first written — a forty-question bank would have the same eight questions doing all the work for every student for ever, and a student redoing a topic would meet the same deck. Salting spreads the load across the bank and still resolves identically every time for a given item.
- **A selector that resolves to nothing is not frozen.** The scope may have been unpublished for a moment, and freezing an empty list would make that permanent.

**Defects found and fixed during the audit** (both in code written in this ticket):
- **The salt broke two of the three orderings, and no test could have caught it.** `$8` is only rendered for the `mixed` ordering, but the caller passed a fixed list of eight arguments — so `easiest_first` and `hardest_first` would have failed on contact with a real connection, which counts placeholders. Fixed structurally rather than by patching the count: `_resolution_query()` now returns the SQL *and* its arguments, so they are decided in one place and cannot drift. A parametrised test now asserts placeholder/argument parity for every ordering × `exclude_seen` combination. **This is the clearest example so far of what the missing database costs** — a fake connection accepts any number of arguments.
- **`item_id` was compared without a cast.** The column is `uuid`; a caller holding the id as a string would have failed to encode. Now `= $1::uuid` with the value passed as text, so either form works.
- One unused import removed.

**Verification actually performed:**
- All six generated resolution query shapes (3 orderings × `exclude_seen`) parse under `sqlglot`'s Postgres dialect, as do the progress query and both rewritten `content.py` queries.
- Placeholder counts checked against argument counts for every shape — the check that found the bug above, now a permanent test.
- `pyflakes` clean across `app/` and `tests/`.
- **Not performed: nothing has been executed against a real Postgres.** Unchanged from JM-1 and JM-2 and now three tickets deep. The argument-count bug above is exactly the class of defect this gap hides, and it was found by inspection rather than by testing. **Running JM-1 through JM-3 against a real database should happen before JM-4 builds endpoints on top of them**, not after.

**Notes for JM-4:**
- `freeze_item` takes the item row and does its own `UPDATE`. It expects to be called inside whatever transaction the endpoint is running; it does not open one.
- `step_progress` needs the frozen ids, which the caller already has from `freeze_item` — so opening a step is one freeze plus one progress read per step, not a query per item.
- A graded step whose selector found nothing reports `pending` and can never complete. JM-4 must decide what to do with it; the sensible answer is to skip it automatically and say why, since blocking a plan on a step with no questions is worse than dropping it.
- `plan_is_complete` returns False for a plan whose every step was skipped. JM-4 should not mark such a plan `completed`.

**Codebase observation, not a JM-3 defect:**
`nodes.parent_id` references `nodes(node_id)` with nothing preventing a cycle, and every recursive descent in this repo — `content.py::_fetch_chapter_subtree`, `scope.py::_SUBTREE_SQL`, and now `resolve.py` — uses `UNION ALL` with no depth guard. A cycle in the tree would hang all three. The prerequisite walk in `scope.py` is guarded because that column is free text; the parent walk is not, in any of them. Worth a small ticket rather than a fix smuggled into this one.
