# Jeene Backend

The backend API for **Jeene**, an AI exam-prep app for NEET.

It serves content (concept trees, questions, figures) from the content database and owns the
app's server-side logic: auth, attempts, grading, weakness detection, the AI tutor, and payments.
The mobile app talks only to this backend; it never touches the database directly.

## Stack

- **Framework:** FastAPI (Python)
- **Database:** Supabase (Postgres), Singapore region
- **Images:** Cloudflare R2, served via CDN; only the URL is stored in the database
- **Hosting:** Render (Singapore)

## Running locally against a database

Until recently this was not possible: the connection pool required TLS, so the only
database the app could talk to was Supabase, and every integration test was written
against that seed. Four tickets were built without a query being executed. Both halves
are fixed — a connection string may now carry its own `sslmode`, and `db/testdata/`
holds a seed you can build a database from.

```bash
brew install postgresql@17
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH" LC_ALL="en_US.UTF-8"
brew services start postgresql@17

createdb jeene_test
psql jeene_test -f ../jeene-plugin/db/schema.sql      # content tables (pipeline-owned)
psql jeene_test -f db/backend_schema.sql              # our tables
psql jeene_test -f db/testdata/seed_plans.sql         # a small, deterministic content set

export DATABASE_URL="postgresql://localhost/jeene_test?sslmode=disable"
.venv/bin/python -m pytest tests/ -q
```

Tests that assert on the real Supabase content (`phy_11_ch4`, its 163 questions) detect
that the seed is absent and skip, so pointing `DATABASE_URL` at a local database does not
produce failures unrelated to what you changed. Everything else — including the whole of
Jeene Mode — runs.

Re-running the seed is a no-op, so you can apply it as often as you like.

## How we build

This repo runs the [Humble Task Force](https://github.com/Humble-Coders/humble-task-force)
product-owner → manager → developer pipeline. Read `docs/PROCESS.md`, then `docs/PRD.md` (the
product) and `CLAUDE.md` (architecture + rules) before starting a ticket.

Secrets are never committed. They live as environment variables on the host and in a local
`.env` (git-ignored).
