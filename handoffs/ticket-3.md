**Ticket:** #3 — [M2] Content read API: chapters, tree, questions, answer reveal

**Summary:**
Adds the read-only content API on top of the Ticket 1 skeleton: six endpoints in a new `app/routers/content.py`, all scoped to the `JEENE_MASTER` tenant. `GET /chapters` lists chapter nodes; `GET /chapters/{id}/tree` walks the self-referencing `nodes` table with a recursive CTE and assembles it into a nested topics → subtopics → concepts tree in Python; `GET /chapters/{id}/questions` and `GET /concepts/{id}/questions` return paginated questions (limit/offset + total count) tagged to a chapter's descendant concepts or to a single concept; `GET /questions/{id}` returns one question; `GET /questions/{id}/answer` is the separate reveal endpoint returning `correct_option_ids`, the worked solution, and concept tags. The question-fetch endpoints' SQL never names `correct_option_ids` or `explanation_json`, enforcing the answer-integrity rule at the query level, not just in the response model. `app/db.py` now registers a `jsonb` type codec on the connection pool so `options_json`/`explanation_json` decode to real JSON objects instead of raw strings.

**Files changed:**
- `app/db.py` — adds `_init_connection`, a pool-init hook that registers a `jsonb` codec (`json.loads`/`json.dumps`) so JSON columns come back as real Python objects, per `CLAUDE.md`'s "serialize JSON columns as real JSON, never as strings" rule; wired into `asyncpg.create_pool` via `init=`.
- `app/schemas.py` — adds `Chapter`, `TreeNode` (recursive), `QuestionFigure`, `Question` (stem/options/difficulty/figures, no answer fields), `ConceptTag`, `QuestionAnswer`, `PaginatedQuestions` — the typed response contract for all six endpoints.
- `app/routers/content.py` (new) — the six endpoints plus shared helpers: `_fetch_chapter_subtree` (recursive CTE over `nodes`, reused by both the tree endpoint and the chapter-questions endpoint to resolve descendant node ids), `_build_tree` (flat rows → nested `TreeNode`), `_paginated_questions_for_node_ids` (shared pagination logic for chapter- and concept-scoped question lists), `_fetch_figures` (batch-fetches `question_figures` for a page of questions to avoid N+1 queries), `_row_to_question`.
- `app/main.py` — registers `content.router` alongside `health.router`.

**How to test:**
1. `source .venv/bin/activate` (or recreate: `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`)
2. Ensure a local `.env` has a real Supabase `DATABASE_URL` (see `.env.example`).
3. `uvicorn app.main:app --reload`
4. `curl http://127.0.0.1:8000/chapters` → expect `phy_11_ch4` / "Laws of Motion".
5. `curl http://127.0.0.1:8000/chapters/phy_11_ch4/tree` → expect 6 topics, each with subtopics, each with concepts (30 concepts total).
6. `curl "http://127.0.0.1:8000/chapters/phy_11_ch4/questions?limit=5"` → expect 5 items, `total: 163`, and confirm `correct_option_ids`/`explanation_json` do not appear anywhere in the response body.
7. `curl http://127.0.0.1:8000/concepts/phy_11_ch4_inertia_first_law_statement/questions` → expect questions tagged to that concept (e.g. `phy_11_ch4_mcq_matching_q1`).
8. `curl http://127.0.0.1:8000/questions/phy_11_ch4_mcq_matching_q3` → expect one question with a populated `figures` array (this question has an R2 image).
9. `curl http://127.0.0.1:8000/questions/phy_11_ch4_mcq_matching_q1/answer` → expect `correct_option_ids`, `explanation`, and `concepts`.
10. `curl http://127.0.0.1:8000/chapters/does_not_exist/tree`, `.../concepts/does_not_exist/questions`, `.../questions/does_not_exist`, `.../questions/does_not_exist/answer` → each expect `404`.

**Acceptance criteria:**
- [x] `GET /chapters` returns `phy_11_ch4` with its title — verified: returns `{"node_id":"phy_11_ch4","title":"Laws of Motion",...}`.
- [x] `GET /chapters/phy_11_ch4/tree` returns the 6 topics, 14 subtopics, and 30 concepts nested correctly — verified by walking the returned tree structure.
- [x] `GET /chapters/phy_11_ch4/questions?limit=5` returns 5 questions with LaTeX stems, options, difficulty, figure URLs where present, and a total count, with no `correct_option_ids`/`explanation_json` anywhere — verified: 5 items returned, `total: 163`, response body confirmed free of both fields.
- [x] `GET /concepts/{a real concept id}/questions` returns the questions tagged to that concept — verified against `phy_11_ch4_inertia_first_law_statement`.
- [x] `GET /questions/{id}` returns one question with no answer; `GET /questions/{id}/answer` returns the correct option(s) + solution — verified against `phy_11_ch4_mcq_matching_q3` (with a figure) and `phy_11_ch4_mcq_matching_q1` (answer reveal).
- [x] Unknown ids return `404` — verified for chapter, concept, question, and question-answer routes.
- [x] Follows `CLAUDE.md`: async throughout, parameterized SQL (`$1`/`$2`/... everywhere, no string formatting), typed Pydantic responses, tenant-scoped (`JEENE_MASTER` constant used in every content query).
- [ ] "Endpoints work on the live Render URL" — not yet verified; this branch hasn't been deployed. Render auto-deploys from `main` on merge (per Ticket 1's `DEPLOY.md`); needs a post-merge check against the public `/health`-adjacent endpoints.

**Deviations / decisions:**
- Node/question `status` is `'draft'` for every row currently in Supabase (confirmed by querying the DB directly). The ticket doesn't ask for status filtering, so none of the queries filter on `status` — filtering it out would make every endpoint return empty. Flagged to the developer before building; explicitly deferred, not an oversight.
- `_fetch_chapter_subtree` is shared between the tree endpoint and the chapter-questions endpoint (both need "all descendant node ids of a chapter") rather than writing the recursive CTE twice.
- Pagination total count uses a separate `COUNT(DISTINCT ...)` query rather than a `COUNT(*) OVER()` window, for clarity; this is one extra round trip per paginated request but keeps the query easier to read and avoids edge cases when a page is empty.
- Figures are batch-fetched per page (`question_id = ANY($1)`) rather than one query per question, to avoid N+1 queries on the list endpoints.

**Open questions / follow-ups:**
- Live Render verification is outstanding — needs merge to `main` (or pointing the Render service at this branch first) plus a manual check of the deployed endpoints, per the last unmet acceptance criterion above.
- No PR opened yet for this branch (`ticket-3-content-read-api`).
