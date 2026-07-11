## 📖 Story / Why
The app's first real job is to show students the syllabus and let them practice. This ticket delivers the read-only **content API** on top of the deployed skeleton: browse chapters, the concept tree, and questions (with LaTeX math and figure URLs), plus a separate endpoint to reveal a question's answer and solution. This is the foundation the app team builds the browsing and practice-review UI on.

## 🧭 Context
- Builds on Ticket 1's deployed skeleton (the `app/` structure, the asyncpg pool, `/health`).
- Content already exists in Supabase, produced by the pipeline: `nodes` (the concept tree), `questions` (`question_text` with LaTeX, `options_json`, `correct_option_ids`, `explanation_json`, `difficulty`, `question_type`), `question_concept_mappings` (each question tagged to concepts, one primary), `question_figures` (`image_url` pointing at R2). All under tenant `JEENE_MASTER`. **This ticket only reads.**
- Per `CLAUDE.md`: async, parameterized SQL, typed Pydantic responses, multi-tenant aware (scope to `JEENE_MASTER`), and the **answer-integrity rule** — question-fetch endpoints must never include `correct_option_ids` or `explanation_json`; those are served only by the dedicated reveal endpoint.
- **Auth:** content endpoints are **public in v1** (no auth yet; auth is Ticket 3). The reveal endpoint is also public for now; it will be tightened once auth + server-side grading land. This is a known, accepted v1 limitation.
- Test chapter already ingested: `phy_11_ch4` (6 topics / 14 subtopics / 30 concepts; 163 questions; 40 figures).

## 🔑 Access & prerequisites
- Same `DATABASE_URL` as Ticket 1 (Supabase session pooler) in a local `.env` and on Render. Obtain from the Manager via a secure channel. Never commit it.

## ✅ Scope / What to build
- [ ] `app/routers/content.py`, all endpoints scoped to `JEENE_MASTER`, all returning typed Pydantic models:
  - [ ] `GET /chapters` — list chapters: `node_id`, `title`, chapter number, subject, class.
  - [ ] `GET /chapters/{chapter_id}/tree` — the chapter's tree nested topics → subtopics → concepts, each with `node_id`, `title`, `description`.
  - [ ] `GET /chapters/{chapter_id}/questions` — questions in a chapter, paginated. Each: `question_id`, `question_type`, stem (LaTeX), options, `difficulty`, and figure `image_url`(s). **No answers.**
  - [ ] `GET /concepts/{concept_id}/questions` — questions tagged to a concept (primary or secondary), paginated, same shape.
  - [ ] `GET /questions/{question_id}` — one question, same shape, no answer.
  - [ ] `GET /questions/{question_id}/answer` — the reveal: `correct_option_ids`, the worked solution (`explanation_json`), and the concept tags. Separate endpoint per the answer-integrity rule.
- [ ] Pydantic schemas in `app/schemas.py`: `Chapter`, `TreeNode` (recursive: children), `Question` (no answer), `QuestionAnswer` (answer + solution).
- [ ] Include each question's figure `image_url`(s) from `question_figures`.
- [ ] Pagination on the two question-list endpoints: `limit` (default 20, max 100) + `offset`; return the total count alongside the page.
- [ ] Register the content router in `app/main.py`.
- [ ] `404` for an unknown chapter / concept / question.

## 🎯 Acceptance Criteria
- [ ] `GET /chapters` returns `phy_11_ch4` with its title.
- [ ] `GET /chapters/phy_11_ch4/tree` returns the 6 topics, 14 subtopics, and 30 concepts nested correctly.
- [ ] `GET /chapters/phy_11_ch4/questions?limit=5` returns 5 questions with LaTeX stems, options, difficulty, figure URLs where present, and a total count — and **no** `correct_option_ids` or `explanation_json` anywhere in the response.
- [ ] `GET /concepts/{a real concept id}/questions` returns the questions tagged to that concept.
- [ ] `GET /questions/{id}` returns one question with no answer; `GET /questions/{id}/answer` returns the correct option(s) + solution.
- [ ] Unknown ids return `404`.
- [ ] Follows `CLAUDE.md`: async, parameterized SQL, typed responses, tenant-scoped. Endpoints work on the live Render URL.

## 🚫 Out of scope
- Auth / gating (Ticket 3); content is public for now.
- Attempts, grading, weakness detection (later).
- Any database writes.

## 🔗 Dependencies
- Ticket 1 (deployed skeleton) merged. ✓

## 📚 References
- `docs/PRD.md` section 5.1 (Content API) and the answer-integrity rule.
- `CLAUDE.md` (structure, non-negotiable rules).

## 🤖 Kickoff prompt (paste into Claude Code)
```
/start-ticket <this-issue-number>
```
