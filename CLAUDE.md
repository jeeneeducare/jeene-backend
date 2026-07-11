# CLAUDE.md — Jeene Backend

Read this on every ticket. It is the architecture and the non-negotiable rules for this repo.
Product spec: `docs/PRD.md`. How we work: `docs/PROCESS.md`.

## What this is
The server-side backend for **Jeene**, an AI exam-prep app for NEET. It is the only thing the
mobile app talks to and the only thing that touches the database. FastAPI (Python) over Supabase
Postgres, deployed on Render (Singapore). It serves content produced by a separate pipeline and,
over time, owns auth, practice, grading, personalization, AI, and payments.

## Stack & infrastructure
- **API:** FastAPI (async), run by uvicorn. Python 3.12+.
- **Database:** Supabase Postgres (Singapore), accessed via an `asyncpg` connection pool.
- **Auth:** Firebase Auth. The app sends a Firebase ID token; the backend verifies it with the
  Firebase Admin SDK and keys user data by the Firebase UID.
- **Images:** Cloudflare R2, served to the app directly by URL via CDN. The backend returns
  `image_url`; it never proxies image bytes.
- **Hosting:** Render (Singapore), co-located with the database.
- **Config/secrets:** every secret is an environment variable (locally a git-ignored `.env`; on
  Render, dashboard env vars), documented by name in `.env.example`. Never commit a secret;
  reference where it lives, never the value.

## Layout (intended; the first tickets establish it)
```
app/
  main.py       # FastAPI app: settings, DB pool lifespan, router registration
  config.py     # settings read from the environment
  db.py         # asyncpg pool + a dependency to acquire a connection
  auth.py       # Firebase token verification (a FastAPI dependency)
  schemas.py    # Pydantic response models (the typed API contract)
  routers/
    health.py   # /health
    content.py  # chapters, tree, questions, answer reveal
requirements.txt
.env.example
```

## Non-negotiable rules
1. **The app talks only to this backend; this backend is the only thing that touches Postgres.**
   Never assume the client reads the database directly.
2. **Content is read-only here.** Never write to the content tables (`nodes`, `questions`,
   `question_concept_mappings`, `question_figures`, `exams`, `exam_syllabus`) — the pipeline owns
   them. The backend writes only its own tables (v1: `users`; later: `attempts`, etc.).
3. **Answer integrity.** Question-fetch endpoints must never select or return
   `correct_option_ids` or `explanation_json`. Correct answers and solutions are served only by the
   dedicated reveal endpoint (and later by server-side grading). This is a correctness rule, not a
   style preference.
4. **SQL is always parameterized** (`$1`, `$2`, ... with asyncpg). Never build SQL with string
   formatting or f-strings. No exceptions.
5. **Everything is async.** No blocking I/O in request handlers; use async DB and HTTP calls.
6. **Multi-tenant aware.** Scope content queries to a tenant (`JEENE_MASTER` for now); never write
   code that silently breaks when a second tenant exists.
7. **Verify auth on protected routes.** User-specific endpoints require a valid Firebase token;
   derive the user from the verified UID, never from a client-supplied id.
8. **Typed responses.** Return Pydantic models so the OpenAPI docs stay an accurate contract for
   the app team.
9. **Secrets never in code, logs, tickets, or this file.** Environment variables only.

## Conventions
- Raw parameterized SQL via `asyncpg`, no ORM for now: the content schema is owned by the pipeline
  and the backend just queries it. Revisit if the backend's own tables grow complex.
- One router module per area under `app/routers/`; keep endpoints thin and push real logic into
  small, testable functions.
- Return clear `HTTPException`s with actionable messages; `404` for missing resources.
- Serialize JSON columns (e.g. `options_json`) as real JSON, never as strings.

## Environment variables
Documented in `.env.example`. At minimum:
- `DATABASE_URL` — Supabase Postgres connection string (session pooler).
- Firebase Admin credentials (service-account JSON or its path) for token verification.
Values are never committed.

## How we work
This repo runs the Humble Task Force pipeline (`docs/PROCESS.md`): the Product Owner frames a brief,
the Manager drafts a ticket (`/draft-ticket`) and reviews the PR (`/manager-review`), the Developer
builds via `/start-ticket` and reports via `/handoff`. Every ticket references `docs/PRD.md` and
this file. Definition of Ready: a junior dev could run the ticket cold.

## References
- `docs/PRD.md` — product spec and decision log.
- `docs/PROCESS.md` — the team workflow.
- Content schema and the pipeline that fills it live in the `jeene-plugin` repo.
