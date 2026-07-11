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

## Verifying the deploy

- Open the service's public URL + `/health`.
- Expect `{"status":"ok","db":"ok"}` with a 200 status.
- If `DATABASE_URL` is missing or wrong, `/health` returns `{"status":"ok","db":"error"}` with a 503 — check the Environment tab and redeploy after fixing it.

## Redeploys

Render redeploys automatically on every push to the connected branch. No manual steps needed after the one-time setup above.
