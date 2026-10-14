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

## Payments (Google Play and the App Store)

Pro is sold only inside the apps: Google Play billing on Android, Apple in-app purchase on
iPhone. The store takes the money; this backend checks every purchase with the store before
granting anything. The console setup (agreements, fee programs, products, keys) is the app
repo's `docs/tickets/002-store-billing-console-setup.md`.

Billing ships **off**: with `JEENE_BILLING_ENABLED` unset, every store route answers 503
and every gate passes. Each store is configured on its own, so one can go live first.

### 1. Apply the schema

Same file, still safe to re-run (`./db/apply_schema.sh` shows what is missing first):

```
./db/apply_schema.sh
```

It adds `store_purchases`, the store product ids on `products`, the private purchase id on
`users`, and the purchase links on `entitlement_grants`. Nothing is dropped and no data is
rewritten. `payments` stays as the record of what Razorpay took.

### 2. Put the store product ids on the passes

```
psql "<DATABASE_URL>" -v ON_ERROR_STOP=1 -f db/store_products.sql
```

It sets `pro_pass_30d` / `pro_pass_90d` / `pro_pass_365d` on the three passes and switches
off the ₹1 `pro_smoke` test pass, then prints the result. The ids must match the consoles
exactly; they can never be changed there.

### 3. Set the environment variables

On the **web service**:

| Key | Value | Needed? |
| --- | --- | --- |
| `JEENE_BILLING_ENABLED` | `true` | **Yes**, or every store route answers 503 |
| `JEENE_RECONCILE_SECRET` | 64 hex characters | **Yes** |
| `GOOGLE_PLAY_PACKAGE_NAME` | `com.jeeneeducareprivatelimited.jeeneeducareprivatelimited` | for Android |
| `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` | the service account's whole JSON key | for Android |
| `GOOGLE_PUBSUB_PUSH_SERVICE_ACCOUNT` | the account the push subscription signs as | for Google's notifications |
| `GOOGLE_PUBSUB_PUSH_AUDIENCE` | `https://jeene-backend.onrender.com/billing/google/notifications` | for Google's notifications |
| `APPLE_BUNDLE_ID` | the iOS bundle id | for iPhone |
| `APPLE_APP_ID` | the app's numeric Apple ID | for real (production) iPhone purchases |
| `APPLE_IAP_KEY_ID`, `APPLE_IAP_ISSUER_ID`, `APPLE_IAP_PRIVATE_KEY` | the In-App Purchase key (whole `.p8`) | for the reconciler's Apple sweep |

Remove `RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`, `RAZORPAY_WEBHOOK_SECRET` and
`JEENE_QUOTE_SECRET`; nothing reads them any more.

On the **cron service** (`jeene-billing-reconciler`): `JEENE_RECONCILE_SECRET`, set to
exactly the same value as on the web service.

### 4. Point the stores at us

- **Google:** a Pub/Sub push subscription on the `play-billing` topic, endpoint
  `https://jeene-backend.onrender.com/billing/google/notifications`, authentication on with
  the service account above; then Play Console → Monetize with Play → Monetization setup →
  Real-time developer notifications, *all notifications for subscriptions and one-time
  products*, and **Send Test Message**. The log should say `Google test notification received`.
- **Apple:** App Store Connect → App Information → App Store Server Notifications →
  Production Server URL `https://jeene-backend.onrender.com/billing/apple/notifications`,
  Version 2. Leave the Sandbox URL empty so test-environment notifications come here too:
  Apple's reviewers buy in the test environment, against this backend.

### What `JEENE_BILLING_ENABLED` actually switches on

Read this before setting it to `true` in production, because it does two things at once.

It turns on **selling** — the store routes stop answering 503 — and it turns on
**locking**. Until it is set, every gate in `app/billing/gates.py` passes and the app
behaves as if everyone were Pro. The moment it is set, students who are not Pro find:

| | Free | Pro |
| --- | --- | --- |
| Practice | 20 questions a rolling day | unlimited |
| Jeene Mode | one plan, ever (reopening it is always free) | the usual three-open cap |
| Mock papers | locked (a sitting already under way can always be finished) | yes |
| Notes and video lectures | the first chapter of each subject | every chapter |
| Mistake Book, progress, the report, browsing the syllabus | always free | always free |

The two are deliberately one switch. A student who meets a paywall on a deployment that
cannot sell to them has hit a dead end. Turning it off again unlocks everything instantly
— entitlements are untouched, so anyone who has paid keeps what they paid for.

Every number above lives in one block at the top of `app/billing/gates.py`.

### How a purchase is confirmed

Three paths, because each fails in a way the others do not, all through one idempotent
module (`app/billing/purchases.py`):

1. **the app** hands the store's proof over (`POST /billing/purchases`) right after paying,
   and again for anything it finds unfinished at launch;
2. **the stores' notifications** — Google's through Pub/Sub, Apple's directly — for
   purchases that complete later (a UPI payment, a parent's Ask to Buy) and for refunds;
3. **the reconciler**, every five minutes: consumes Google purchases before Google refunds
   anything unacknowledged for three days, settles pending ones, applies refunds nobody
   announced, and reads back Apple's undelivered notifications.

A refund takes the pass's days back with a reversing grant, once, however many times it is
reported. Test purchases (Apple's test environment, Google's license testers) grant Pro like
real ones and are recorded with `environment = 'sandbox'`; leave them out of revenue:

```sql
SELECT store, count(*), sum(price_micros) / 1000000 AS rupees
  FROM store_purchases
 WHERE environment = 'production' AND status = 'granted'
 GROUP BY store;
```

## Ask Jeene (doubt solving)

Separate from Jeene Mode in every way that matters. Jeene Mode plans a chapter; Ask Jeene
answers one doubt about the chapter a student is already reading, from that chapter's own
concepts, worked solutions and notes. They share a provider port and nothing else — their
flags, their models and their failure modes are all independent, on purpose.

### 1. Apply the schema

```bash
psql "<the same DATABASE_URL Render uses>" -f db/backend_schema.sql
```

Adds `doubt_threads` and `doubt_messages`, and sets `doubt_messages.created_at` to default
to `clock_timestamp()`. That last one is not cosmetic: a question and its answer are
written in one transaction, `now()` is the transaction's start time, and two rows sharing
an instant order by their random uuid — which shows a student the answer above the question
it answers.

Safe to re-run, like the rest of the file.

### 2. Set the environment variables

| Key | Value | Needed? |
| --- | --- | --- |
| `OPENAI_API_KEY` | the project key | **Yes** — the same key Jeene Mode uses |
| `JEENE_DOUBTS_MODEL` | `gpt-5-mini` | **Yes** |
| `JEENE_DOUBTS_ENABLED` | `true` | **Yes**, or `/doubts/ask` answers 503 and says so |

Leaving any of the three unset is a supported configuration: the route answers 503 with
"Ask Jeene is not available right now", and every other part of the chapter still works.
It is also the switch to reach for if answers start going wrong — turn
`JEENE_DOUBTS_ENABLED` off and nothing else changes.

**On the model.** Measured on real chapter material, four doubts each, at the ten-a-day
cap:

| | per answer | per heavy Pro student | latency |
| --- | --- | --- | --- |
| `gpt-5` | ₹0.82 | ₹247/month | 12–21s |
| `gpt-5-mini` | ₹0.10 | ₹30/month | 6.5–10s |

Quality was close enough that the eight-times cost difference decided it. Moving up is one
environment variable if answers disappoint — and because the model id is recorded on every
answer, you can tell afterwards which ones were written by which.

### 3. What the gate does

Pro only, and ten doubts a day even for Pro. The two halves answer to different things and
only one of them follows `JEENE_BILLING_ENABLED`:

* **Behind Pro** — a monetisation decision, so it turns off with billing, like every other
  gate. With billing off, anybody can ask.
* **Ten a day** — not a monetisation decision. Every doubt is a model call billed to a real
  account, and a deployment with billing off is exactly the one where nobody is watching
  that bill. The cap applies either way.

A refused student has spent nothing: the gate runs before the model and the record is
written after it.

### 4. Content prerequisites

An answer is built only from material that exists, and a chapter with nothing in it is
refused before the call rather than after — an empty material block is how you get
confident invention. In practice a chapter needs published concepts **with descriptions**;
worked solutions and notes are used when they are there.

All 1,517 published concepts have descriptions today, so every chapter qualifies. Notes
exist for eight physics chapters and are used where present.

### 5. Watching it

```bash
curl -H "X-Jeene-Reconcile: $JEENE_RECONCILE_SECRET" \
  "https://jeene-backend.onrender.com/internal/doubts/health?days=7"
```

Behind the cron secret, the same one the reconciler uses, and read-only. Nothing it
returns is the text of anybody's question, but the shape of a student's week is not public
either.

Two numbers matter, and they are different in kind.

**`refusal_rate`** is a signal. Near zero means Jeene is answering past what the material
supports — inventing. Very high means the material is too thin to be worth asking. The
evaluation set sits at 50% by construction, because half its cases are things that must be
refused; real traffic should land well below that.

**`ungrounded`** is a bug. It counts answers thrown away for citing material they were
never given, and it should read zero. A rising count is the prompt or the material block
having drifted, not a student asking something awkward. Each one is logged as
`doubt answer dropped reason=cited material it was not given` with the offending
references and never the text.

`reported` is students tapping "this was wrong", which is the best bug report this feature
will get — an answer is assembled from one student's material at one moment, and without
that tap nobody would ever learn which ones were bad. Read them:

```sql
SELECT message_id, left(text, 200) FROM doubt_messages
 WHERE reported ORDER BY created_at DESC;
```

### 6. The evaluation set

```bash
DATABASE_URL="<production, read-only>" JEENE_DOUBTS_MODEL=gpt-5-mini \
  .venv/bin/python evals/doubts/run.py
```

Twelve labelled doubts against real chapter material in all three subjects — six it must
answer, six it must refuse, including a request for the answer key, a jailbreak and a
message from a student in distress. It reports grounding, whether it answered and refused
the right things, whether the maths will render, and what an answer costs.

It costs about ₹2.50 a run and reads production without writing to it. Run it after any
change to the prompt, the schema, or the material block — all three are places where an
edit that reads better can quietly make answers worse, and nothing else will tell you.

Three consecutive runs on `gpt-5-mini`: 12/12 grounded, 12/12 correctly answered or
refused, 12/12 renderable, every time. ₹0.19 an answer cold, ₹0.08 once the provider's
prefix cache is warm — so ₹24 a month for a student who uses all ten every day, which
almost nobody will. Latency 3.6–15s, with an occasional long one past thirty; the client
waits sixty before giving up.

The figures above are also the reason the model is `gpt-5-mini` rather than `gpt-5`: on
the same cases `gpt-5` cost eight times as much and took roughly twice as long, at quality
this set could not tell apart.
