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

## How we build

This repo runs the [Humble Task Force](https://github.com/Humble-Coders/humble-task-force)
product-owner → manager → developer pipeline. Read `docs/PROCESS.md`, then `docs/PRD.md` (the
product) and `CLAUDE.md` (architecture + rules) before starting a ticket.

Secrets are never committed. They live as environment variables on the host and in a local
`.env` (git-ignored).
