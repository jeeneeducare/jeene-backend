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
| `JEENE_NOTES_STORAGE_HOSTS` | *leave unset* | Only for a local fixture server |

`JEENE_NOTES_STORAGE_HOSTS` is deliberately blank in production. Unset, the notes viewer
will stream only from an `https` URL that resolves to a public address — which is stricter
than any list, and does not go stale when storage moves.

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

## Payments (Razorpay)

Nothing here is optional-with-a-fallback the way Jeene Mode is. Billing ships **off**:
with `JEENE_BILLING_ENABLED` unset, every money route answers 503 and says so. Turn it on
only once all four steps below are done, in this order.

### 1. Apply the schema

Same file, still safe to re-run:

```
psql "<the same DATABASE_URL Render uses>" -f db/backend_schema.sql
```

That adds `products`, `payments`, `entitlement_grants` and `users.pro_expires_at`. Nothing
is dropped and no data is rewritten.

### 2. Set the environment variables

On the **web service**:

| Key | Value | Needed? |
| --- | --- | --- |
| `JEENE_BILLING_ENABLED` | `true` | **Yes**, or every money route answers 503 |
| `RAZORPAY_KEY_ID` | `rzp_test_…` while testing, `rzp_live_…` after | **Yes** |
| `RAZORPAY_KEY_SECRET` | the matching secret | **Yes** |
| `JEENE_QUOTE_SECRET` | 64 hex characters from `python3 -c "import secrets; print(secrets.token_hex(32))"` | **Yes** |
| `RAZORPAY_WEBHOOK_SECRET` | issued in step 3 | **Yes**, before going live |
| `JEENE_RECONCILE_SECRET` | another 64 hex characters | **Yes** |

Start in **test mode**. A test key id and a live key id differ by one word and produce
identical-looking behaviour right up until real money moves.

On the **cron service** (`jeene-billing-reconciler`, created from the blueprint):
`JEENE_RECONCILE_SECRET`, set to exactly the same value as on the web service.

### 3. Register the webhook

In the Razorpay dashboard, **Settings → Webhooks → Add New Webhook**:

- URL: `https://jeene-backend.onrender.com/billing/webhook`
- Events: `payment.captured`, `payment.failed`, `order.paid`
- Secret: generate one, and put the same value in `RAZORPAY_WEBHOOK_SECRET` on Render.

The secret is what authenticates the webhook — that endpoint takes no user token, because
Razorpay has none to give. Until `RAZORPAY_WEBHOOK_SECRET` is set, every webhook is
rejected as unsigned, and payments are confirmed only by the app and the reconciler.

### 4. Create the products

Prices live in the database, not in the app and not in Razorpay. Seed the three passes:

```
psql "<DATABASE_URL>" -f db/testdata/seed_products.sql
```

or create them from the admin panel (`POST /admin/products`). Retiring one sets
`active = false`; a product is never deleted, because a payment row points at it and a
receipt has to keep making sense.

### What `JEENE_BILLING_ENABLED` actually switches on

Read this before setting it to `true` in production, because it does two things at once.

It turns on **selling** — the money routes stop answering 503 — and it turns on
**locking**. Until it is set, every gate in `app/billing/gates.py` passes and the app
behaves exactly as it does today. The moment it is set, existing students who are not Pro
find:

| | Free | Pro |
| --- | --- | --- |
| Practice | 20 questions a rolling day | unlimited |
| Jeene Mode | one plan, ever (reopening it is always free) | the usual three-open cap |
| Mock papers | locked (a sitting already under way can always be finished) | yes |
| Notes and video lectures | the first chapter of each subject | every chapter |
| Mistake Book, progress, the report, browsing the syllabus | always free | always free |

The two are deliberately one switch. A student who meets a paywall on a deployment that
cannot take their money has hit a dead end, and that is a worse first impression than
anything on the other side of it.

So: set the Razorpay variables, walk the test-mode matrix, ship the apps that understand
a `402`, and only then set this. Turning it off again unlocks everything instantly —
entitlements are untouched, so anyone who has paid keeps what they paid for.

Every number above lives in one block at the top of `app/billing/gates.py`.

### How a payment is confirmed

Four independent paths, because each fails in a way the others do not, and all four go
through one idempotent mutator (`app/billing/settle.py`):

1. **the app**, on the checkout SDK's success callback — signature checked, then the
   payment fetched server-to-server, because a signature proves the callback is genuine
   and not that money moved;
2. **the webhook**, signed over the raw body;
3. **a failure report** from the app, which is ignored outright if Razorpay says
   something was captured;
4. **the reconciler**, every five minutes, asking about everything still pending.

A payment that is captured after we have already given up on it goes to
`needs_manual_review` rather than quietly to `paid`. Watch for those:

```sql
SELECT order_id, firebase_uid, amount_paise, failure_reason, updated_at
  FROM payments WHERE status = 'needs_manual_review' ORDER BY updated_at DESC;
```

Each one is a person deciding between granting access and refunding — by then the student
has been told it failed and may well have paid again.
