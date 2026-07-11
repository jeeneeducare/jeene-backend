**Ticket:** #1 — [M1] FastAPI skeleton + Supabase + /health, deployed to Render

**Summary:**
Adds the first working slice of the backend: an `app/` package with a FastAPI application, an async `asyncpg` connection pool to Supabase Postgres, and a single `GET /health` endpoint that confirms end-to-end reachability with a `SELECT 1`. The pool is opened on app startup and closed on shutdown via a FastAPI lifespan. `/health` returns `200 {"status":"ok","db":"ok"}` when the database is reachable, and `503 {"status":"ok","db":"error"}` — never a stack-trace crash — when `DATABASE_URL` is unset, malformed, or the database is unreachable. Also adds `requirements.txt`, `.env.example`, a `render.yaml` blueprint for a Singapore-region web service, and `DEPLOY.md` documenting the Render setup steps.

**Files changed:**
- `app/main.py` — FastAPI app; lifespan opens/closes the DB pool on startup/shutdown; registers the health router.
- `app/config.py` — `Settings` (pydantic-settings) reads `DATABASE_URL` from the environment/`.env`; optional so the app can start even without it (needed for the clean-503 requirement).
- `app/db.py` — owns the module-level asyncpg pool: `connect_pool()` / `disconnect_pool()` for lifespan use, `get_pool_or_none()` for the health check, `get_pool()` / `get_connection()` as the dependency future routers will use. Pool creation uses `ssl="require"` and `statement_cache_size=0` (required for Supabase's session pooler) and swallows connection failures into `_pool = None` instead of raising, so a bad/unset `DATABASE_URL` doesn't crash app startup.
- `app/routers/health.py` — `GET /health`: returns `db:"error"` + 503 if the pool never came up or the `SELECT 1` call raises; otherwise `db:"ok"` + 200.
- `app/schemas.py` — `HealthResponse` Pydantic model (typed response contract per `CLAUDE.md` rule 8).
- `app/__init__.py`, `app/routers/__init__.py` — package markers.
- `requirements.txt` — `fastapi`, `uvicorn[standard]`, `asyncpg`, `pydantic-settings`.
- `.env.example` — documents `DATABASE_URL` by name only, no value.
- `render.yaml` — Python web service blueprint, `region: singapore`, pip-install build command, uvicorn start command, `DATABASE_URL` declared with `sync: false` (dashboard-set, not synced from repo).
- `DEPLOY.md` — step-by-step Render Blueprint setup, environment variable configuration, and deploy verification instructions.

**How to test:**
1. `python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
2. Create a local `.env` with a real Supabase session-pooler `DATABASE_URL` (see `.env.example` for the key name).
3. `uvicorn app.main:app --reload`
4. `curl http://127.0.0.1:8000/health` → expect `200` and `{"status":"ok","db":"ok"}`.
5. Unset or corrupt `DATABASE_URL` (e.g. point it at an unreachable host/port) and repeat step 3–4 → expect `503` and `{"status":"ok","db":"error"}`, with no exception/stack trace in the server log.

**Acceptance criteria:**
- [x] Locally, with `DATABASE_URL` set, `uvicorn app.main:app` starts and `GET /health` returns 200 `{"status":"ok","db":"ok"}` — verified against a real Supabase session-pooler URL.
- [x] With a bad or unset `DATABASE_URL`, `/health` returns a clear 503, not a stack-trace crash — verified both the unset case and a bad/unreachable connection string.
- [x] No secrets committed; `.env` is git-ignored (pre-existing `.gitignore` entry); `.env.example` lists names only.
- [x] Code follows `CLAUDE.md`: async throughout, parameterized SQL (`SELECT 1` via asyncpg, no string formatting), typed Pydantic response.
- [x] `render.yaml` is present and valid.
- [ ] Service created from `render.yaml` and deployed live on Render (Singapore); public `/health` confirmed green — not yet done, requires the developer's Render workspace access. Steps are documented in `DEPLOY.md`.

**Deviations / decisions:**
- The ticket's `Settings` field for `DATABASE_URL` is typed `str | None` (optional) rather than required. A required field would make pydantic-settings raise at import time when the env var is unset, which is an application-startup crash rather than the clean per-request 503 the acceptance criteria call for. Making it optional and having `db.py` treat "unset" and "unreachable" the same way (pool stays `None`) satisfies the "bad or unset → clean 503, no crash" criterion for both cases uniformly.
- `render.yaml` uses `plan: starter` since the ticket didn't specify a plan tier; this is easy to change in the Render dashboard or blueprint later.

**Open questions / follow-ups:**
- Render deploy itself (service creation, setting `DATABASE_URL` in the dashboard, confirming the public `/health`) is still outstanding and needs to be done by whoever holds `jeeneeducare` Render workspace access, per `DEPLOY.md`.
- PR already opened: https://github.com/jeeneeducare/jeene-backend/pull/2
