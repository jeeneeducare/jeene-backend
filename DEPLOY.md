# Deploying to Render

The Jeene backend deploys to Render (Singapore), co-located with the Supabase database.

## One-time setup

1. Log in to the `jeeneeducare` Render workspace (get access from the Manager if you don't have it).
2. **New > Blueprint**, connect this GitHub repo, and select the branch to deploy (`main`).
3. Render reads `render.yaml` at the repo root and proposes a web service named `jeene-backend` in the Singapore region. Confirm creation.
4. Once the service exists, go to its **Environment** tab and add:
   - `DATABASE_URL` = the Supabase session-pooler connection string (get it from the Manager via a secure channel, or Supabase dashboard → Connect → Session pooler).
   Do **not** put this value in `render.yaml` or anywhere in the repo — it's dashboard-only (`sync: false` in the blueprint).
5. Save. Render will build (`pip install -r requirements.txt`) and start (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`) the service automatically.

## Jeene Mode (added after the first deploy)

Jeene Mode needs three things beyond the original setup. Do them in this order.

### 1. Apply the schema

`db/backend_schema.sql` is safe to re-run — every statement is `IF NOT EXISTS` or an
`ADD COLUMN IF NOT EXISTS`. Run the whole file against the production database:

```
psql "<the same DATABASE_URL Render uses>" -f db/backend_schema.sql
```

That creates `study_plans`, `study_plan_steps`, `study_plan_step_items` and
`chapter_notes`, and adds `study_plan_steps.remediation_round` and
`node_videos.duration_seconds` to anything already there. Nothing is dropped and no data
is rewritten, so it is safe on a live database.

### 2. Set the environment variables

In the service's **Environment** tab:

| Key | Value | Needed? |
| --- | --- | --- |
| `JEENE_ASSET_SECRET` | 64 hex characters from `python3 -c "import secrets; print(secrets.token_hex(32))"` | **Yes.** Without it the notes viewer fails intermittently across workers. |
| `OPENAI_API_KEY` | the project key | Only for model-written plans |
| `JEENE_PLANNER_MODEL` | e.g. `gpt-5` | Only for model-written plans |
| `JEENE_PLANNER_ENABLED` | `true` | Only for model-written plans |

Leaving the last three unset is a **supported configuration**: every plan then comes from
the deterministic planner, no network call is made, and nothing is billed. It is also the
switch to reach for if the model starts producing bad plans — turn `JEENE_PLANNER_ENABLED`
off and plans keep working.

### 3. Content prerequisites

A plan can only point at material that exists. Per scope the planner needs published
questions mapped to concepts, and it will use a video (`node_videos`) or chapter notes
(`chapter_notes`) if they are there. Notes currently exist for eight physics chapters and
nowhere else; a scope with no notes simply gets a plan without a notes step.

`chapter_notes.pdf_url` must be **reachable from the Render service**, because the notes
viewer streams the file through the API rather than handing the URL to the app.

## Verifying the deploy

- Open the service's public URL + `/health`.
- Expect `{"status":"ok","db":"ok"}` with a 200 status.
- If `DATABASE_URL` is missing or wrong, `/health` returns `{"status":"ok","db":"error"}` with a 503 — check the Environment tab and redeploy after fixing it.

## Redeploys

Render redeploys automatically on every push to the connected branch. No manual steps needed after the one-time setup above.
