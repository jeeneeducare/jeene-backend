## 📖 Story / Why
The backend is the app's single interface and the only thing that touches the database. Before any content or auth logic, we need a running, deployable FastAPI app that connects to Supabase and proves the whole chain works end to end. This walking skeleton establishes the project structure from `CLAUDE.md` and the deploy path, so every later ticket builds on a live, known-good base.

## 🧭 Context
- New repo, currently only docs (`docs/PRD.md`, `CLAUDE.md`, `docs/PROCESS.md`) and HTF scaffolding.
- Stack per `CLAUDE.md`: FastAPI (async, Python 3.12+), an `asyncpg` pool to Supabase Postgres (Singapore), deployed on Render (Singapore), co-located with the DB.
- The content database already exists in Supabase (7 tables, `phy_11_ch4` ingested). This ticket does **not** query content, it only confirms the DB is reachable with `SELECT 1`.
- **Deploy split:** the Manager owns the Render account and secrets. The developer delivers deployable code, a `render.yaml` blueprint, `.env.example`, and a short `DEPLOY.md`. The Manager creates the Render service from the blueprint and pastes in `DATABASE_URL`.

## 🔑 Access & prerequisites
- **`DATABASE_URL`** — the Supabase connection string (session pooler). Get it from the existing local `~/.jeene.env` (`JEENE_POSTGRES_URL`) or Supabase dashboard → Connect → Session pooler. **Never commit it.**
- **Render account** — the Manager sets up the service; the developer only provides config and steps.
- Python 3.12+ locally.

## ✅ Scope / What to build
- [ ] `app/` package: `main.py` (FastAPI app, asyncpg pool lifespan, router registration), `config.py` (settings from env), `db.py` (pool + a connection dependency), `routers/health.py`.
- [ ] Async `asyncpg` connection pool to Supabase: `DATABASE_URL` from env, SSL enabled, `statement_cache_size=0` (pooler-safe).
- [ ] `GET /health` — returns `{"status":"ok","db":"ok"}`, running a trivial `SELECT 1` to confirm the DB is reachable; returns `db:"error"` + HTTP 503 if the DB is unreachable (no crash).
- [ ] `requirements.txt` (fastapi, uvicorn[standard], asyncpg).
- [ ] `.env.example` documenting `DATABASE_URL` by name only (no value).
- [ ] `render.yaml` blueprint: a Python web service, Singapore region, pip-install build + uvicorn start commands, `DATABASE_URL` declared as a dashboard-set env var (not synced from the repo).
- [ ] `DEPLOY.md` with click-by-click Render setup steps for the Manager.

## 🎯 Acceptance Criteria
- [ ] Locally, with `DATABASE_URL` set, `uvicorn app.main:app` starts and `GET /health` returns 200 `{"status":"ok","db":"ok"}`.
- [ ] With a bad or unset `DATABASE_URL`, `/health` returns a clear 503, not a stack-trace crash.
- [ ] No secrets committed; `.env` is git-ignored; `.env.example` lists names only.
- [ ] Code follows `CLAUDE.md`: async throughout, parameterized SQL, typed responses.
- [ ] `render.yaml` is present and valid; the Manager can create the Render service from it and set `DATABASE_URL`.
- [ ] Deployed live on Render (Singapore): the public `/health` URL returns ok. *(Joint step: developer provides config + `DEPLOY.md`; Manager creates the service and sets the secret.)*

## 🚫 Out of scope
- Any content endpoints (Ticket 2).
- Firebase auth (Ticket 3).
- Any database writes or user tables.

## 🔗 Dependencies
- Supabase project exists and is reachable (done).
- Render account (Manager).

## 📚 References
- `docs/PRD.md` — v1 scope and system architecture.
- `CLAUDE.md` — project structure and non-negotiable rules.

## 🤖 Kickoff prompt (paste into Claude Code)
```
/start-ticket <this-issue-number>
```
