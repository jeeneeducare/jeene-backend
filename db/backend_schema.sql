-- Backend-owned tables. The content tables (nodes/questions/...) are owned by the
-- pipeline; the backend owns only its own tables. v1: users. Later: attempts, etc.

CREATE TABLE IF NOT EXISTS users (
  firebase_uid   TEXT PRIMARY KEY,
  tenant_id      TEXT NOT NULL DEFAULT 'JEENE_MASTER' REFERENCES tenants(tenant_id),
  display_name   TEXT,
  email          TEXT,
  phone          TEXT,
  class_level    INTEGER,
  target_exam    TEXT,
  auth_provider  TEXT,
  photo_url      TEXT,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_users_tenant ON users (tenant_id);

-- Attempts: an append-only event log of every answer a student submits.
--
-- Append-only on purpose. The same question answered three times is three rows;
-- collapsing to one row per student+question would destroy streaks, "you got this
-- wrong twice", spaced repetition and honest time analytics.
--
-- Mastery and weakness are DERIVED from this table, never stored alongside it, so
-- there is one source of truth. If aggregation gets slow at scale, a rollup table
-- goes on top without changing what is recorded here.
CREATE TABLE IF NOT EXISTS attempts (
  -- Client-generated so a retried request cannot double-count an answer.
  attempt_id          UUID PRIMARY KEY,
  firebase_uid        TEXT NOT NULL REFERENCES users(firebase_uid) ON DELETE CASCADE,
  -- Denormalised so B2B analytics ("how is this coaching's batch doing") never has
  -- to join back through users.
  tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
  question_id         TEXT NOT NULL REFERENCES questions(question_id),
  -- Groups the attempts of one timed test together. Nullable: practice has no
  -- session. The FK arrives with the sessions table when tests are built.
  session_id          UUID,

  selected_option_ids TEXT[],
  numeric_answer      NUMERIC,
  -- Graded server-side against the hidden key; never sent by the client.
  is_correct          BOOLEAN NOT NULL,
  -- Cannot be backfilled, so it is recorded from the very first attempt.
  time_spent_ms       INTEGER,
  -- Whether the student saw the worked solution for this attempt, so mastery can
  -- discount answers that were revealed rather than earned.
  solution_revealed   BOOLEAN NOT NULL DEFAULT false,

  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_attempts_user_time     ON attempts (firebase_uid, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_attempts_user_question ON attempts (firebase_uid, question_id);
CREATE INDEX IF NOT EXISTS idx_attempts_question      ON attempts (question_id);
CREATE INDEX IF NOT EXISTS idx_attempts_tenant_user   ON attempts (tenant_id, firebase_uid);
CREATE INDEX IF NOT EXISTS idx_attempts_session       ON attempts (session_id) WHERE session_id IS NOT NULL;

-- Test sessions: one student's sitting of one paper.
--
-- The answers themselves are ordinary rows in `attempts`, carrying this session_id.
-- That is why attempts.session_id was added nullable from the start: practice has no
-- session, a test does, and both are the same log for analytics — so a paper's
-- responses feed weakness detection exactly like practice does.
--
-- The handoff code is what lets a student start on the phone and sit the paper in a
-- browser. It is a credential for THIS session, not a paper selector: it is bound to
-- the student who created it, so a stranger with the code cannot use it.
CREATE TABLE IF NOT EXISTS test_sessions (
  session_id    UUID PRIMARY KEY,
  test_id       TEXT NOT NULL REFERENCES tests(test_id),
  firebase_uid  TEXT NOT NULL REFERENCES users(firebase_uid) ON DELETE CASCADE,
  tenant_id     TEXT NOT NULL REFERENCES tenants(tenant_id),

  -- Short, human-typeable handle for moving this sitting to a browser. Single use:
  -- once claimed it stops working, so a code glimpsed later is already dead.
  handoff_code  TEXT UNIQUE,
  handoff_claimed_at TIMESTAMPTZ,
  -- Returned when the code is claimed, and the browser's credential thereafter.
  -- Deliberately separate from session_id: an identifier that appears in URLs and
  -- logs should not also be a key. Grants exactly this sitting and nothing else.
  web_token     TEXT UNIQUE,

  started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- The clock is derived from started_at plus the paper's duration, never from
  -- counted ticks, so backgrounding a tab or app cannot buy extra minutes.
  expires_at    TIMESTAMPTZ NOT NULL,
  submitted_at  TIMESTAMPTZ,

  -- Filled at submission, from the recorded attempts and the paper's marking scheme.
  score         NUMERIC,
  correct_count INTEGER,
  wrong_count   INTEGER,
  skipped_count INTEGER,

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_test_sessions_user ON test_sessions (firebase_uid, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_test_sessions_test ON test_sessions (test_id);

-- The answer sheet for one sitting: what the student has filled in so far.
--
-- Deliberately NOT the attempt log. During a test an answer is a draft the student
-- may change any number of times, and recording each change as an attempt would
-- corrupt the very analytics the log exists for: revise A -> B -> C and a question
-- you finally got right reads as two wrong answers and one correct, so a careful
-- student scores worse than a lucky one and weakness detection flags concepts they
-- actually know. It would also creep their progress rings upward mid-paper.
--
-- So the sheet is updated in place while sitting, and at submission the final state
-- of each question becomes exactly one graded attempt.
CREATE TABLE IF NOT EXISTS test_responses (
  session_id        UUID NOT NULL REFERENCES test_sessions(session_id) ON DELETE CASCADE,
  question_id       TEXT NOT NULL REFERENCES questions(question_id),

  selected_option_ids TEXT[],
  numeric_answer      NUMERIC,
  -- Presentation state, kept with the response so it survives a device change.
  marked_for_review   BOOLEAN NOT NULL DEFAULT false,
  -- How often the student changed their mind here. Genuine evidence of shakiness,
  -- recorded without letting it distort accuracy.
  revision_count      INTEGER NOT NULL DEFAULT 0,

  first_answered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  -- Time on this question accumulates across visits.
  time_spent_ms     INTEGER NOT NULL DEFAULT 0,

  PRIMARY KEY (session_id, question_id)
);

CREATE INDEX IF NOT EXISTS idx_test_responses_session ON test_responses (session_id);

-- One plain-language explanation per question, written by a model from the teacher's
-- own worked solution and shown under "Understand with AI".
--
-- Keyed on the question, not the student: every student who asks about a question sees
-- the same explanation, so the cost and the load are bounded by the size of the bank
-- rather than by how many students there are. Nothing is generated while a student
-- waits; these are written by the pipeline and read back as ordinary rows.
CREATE TABLE IF NOT EXISTS question_explanations (
    question_id     TEXT PRIMARY KEY REFERENCES questions(question_id) ON DELETE CASCADE,
    tenant_id       TEXT NOT NULL,
    text            TEXT NOT NULL,
    -- Which model and which prompt produced it. Improving the prompt is a deliberate
    -- act: bump the version and regenerate, rather than leaving a bank half-written by
    -- one prompt and half by another with no way to tell which is which.
    model           TEXT NOT NULL,
    prompt_version  INT  NOT NULL,
    -- A hash of the question, its options and its worked solution. When a teacher
    -- corrects a solution the hash moves, and the explanation derived from the old one
    -- is stale — an explanation of a correction nobody made is worse than none.
    source_hash     TEXT NOT NULL,
    -- Follows the same draft-then-publish path as every other piece of content here.
    status          TEXT NOT NULL DEFAULT 'draft'
                    CHECK (status IN ('draft', 'published')),
    tokens_in       INT,
    tokens_out      INT,
    generated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at     TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_question_explanations_status
    ON question_explanations (status);

-- Written notes for a chapter, as a hosted PDF.
--
-- Reconstructed from the queries in `content.py`, which has read this table since notes
-- shipped. It existed in the live database and in no schema file, which meant a database
-- built from version control could not serve `/chapters/{id}/notes` at all — and, later,
-- could not build a study-plan inventory. Found by standing up a fresh database from
-- this repo and watching the query fail.
--
-- The live table is the authority on anything this gets wrong: `IF NOT EXISTS` means
-- production is untouched, so if the two have drifted this is the copy to correct.
CREATE TABLE IF NOT EXISTS chapter_notes (
    chapter_id  text PRIMARY KEY REFERENCES nodes(node_id),
    tenant_id   text NOT NULL,
    title       text NOT NULL,
    -- What the reader opens. Never described to a planning model, and from JM-9 served
    -- through our own viewer rather than handed to the client.
    pdf_url     text NOT NULL,
    page_count  integer,
    size_bytes  bigint,
    status      text NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'published')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_chapter_notes_status ON chapter_notes (tenant_id, status);

-- The notes as text, for Ask Jeene to quote. The reader streams the PDF and the student
-- does the reading; a doubt solver cannot, and may only ground an answer in material it
-- has been handed. Extracted once per document and refreshed when the row is touched.
--
-- `text_extracted_at` is null until it has been tried. Set with an empty `extracted_text`
-- it means the document was read and held no text — an image-only PDF — which is recorded
-- so it is attempted once rather than on every question about that chapter.
ALTER TABLE chapter_notes ADD COLUMN IF NOT EXISTS extracted_text    TEXT NOT NULL DEFAULT '';
ALTER TABLE chapter_notes ADD COLUMN IF NOT EXISTS text_extracted_at TIMESTAMPTZ;


-- Videos, at any level of the tree.
--
-- Supersedes chapter_videos. A link that explains one topic belongs on that topic, and
-- one that covers the whole chapter belongs on the chapter; the old table could only say
-- chapter. Reads fall back up the tree, so a topic with no video of its own still shows
-- its chapter's.
CREATE TABLE IF NOT EXISTS node_videos (
    node_id       text NOT NULL REFERENCES nodes(node_id),
    youtube_id    text NOT NULL,
    tenant_id     text NOT NULL,
    title         text NOT NULL,
    channel       text NOT NULL DEFAULT '',
    thumbnail_url text NOT NULL DEFAULT '',
    position      integer NOT NULL DEFAULT 0,
    status        text NOT NULL DEFAULT 'draft',
    -- Who attached it. An admin panel with no name against each change is a panel nobody
    -- can be asked about a mistake in.
    added_by      text NOT NULL DEFAULT '',
    added_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (node_id, youtube_id)
);

CREATE INDEX IF NOT EXISTS node_videos_lookup
    ON node_videos (tenant_id, node_id, status, position);

-- Who may write content through the admin panel.
--
-- Every student holds a valid Firebase token, so a token alone can never be the check.
-- Membership is a row here, which means access is granted and revoked with one statement
-- and is visible to anyone who looks, rather than living in a claim nobody can enumerate.
CREATE TABLE IF NOT EXISTS admins (
    firebase_uid text PRIMARY KEY,
    tenant_id    text NOT NULL,
    email        text NOT NULL DEFAULT '',
    note         text NOT NULL DEFAULT '',
    added_at     timestamptz NOT NULL DEFAULT now()
);

-- Admin invites: access granted to an email before that person has an account.
--
-- `admins` is keyed by Firebase uid, which is the right identity to check against: an
-- email can be reassigned on a Google Workspace account and a uid cannot. But a uid does
-- not exist until somebody has signed in, so granting access to a new colleague meant
-- asking them to sign in, be refused, and wait for a second step.
--
-- An invite is that grant, held by email until there is a uid to attach it to. It is
-- claimed on the first request that arrives with a verified token for that address, and
-- deleted in the same transaction, so it is a one-time key rather than a standing rule.
CREATE TABLE IF NOT EXISTS admin_invites (
    email      text PRIMARY KEY,
    tenant_id  text NOT NULL,
    note       text NOT NULL DEFAULT '',
    invited_by text NOT NULL DEFAULT '',
    invited_at timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Jeene Mode: a student's route through one scope.
--
-- A plan is keyed to a *scope node* of any plannable type, not to a chapter. A plan
-- that pulls in a prerequisite from an earlier chapter is the normal case rather than
-- the exception, so "which chapter is this plan about" is a question with no answer and
-- the schema does not pretend otherwise.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS study_plans (
  plan_id           UUID PRIMARY KEY,
  firebase_uid      TEXT NOT NULL REFERENCES users(firebase_uid) ON DELETE CASCADE,
  tenant_id         TEXT NOT NULL REFERENCES tenants(tenant_id),

  scope_node_id     TEXT NOT NULL REFERENCES nodes(node_id),
  scope_type        TEXT NOT NULL CHECK (scope_type IN ('chapter', 'topic', 'subtopic')),
  -- Denormalised on purpose: a plan made in August must still read correctly after the
  -- tree is retitled in September.
  scope_title       TEXT NOT NULL,

  proficiency       TEXT NOT NULL
                    CHECK (proficiency IN ('basic', 'intermediate', 'advanced')),
  intent            TEXT NOT NULL
                    CHECK (intent IN ('first_time', 'revising', 'exam_soon')),

  -- How this plan was produced, so "why did this student get a thin plan" is answerable
  -- without guessing. 'fallback' is the deterministic planner, which is not a failure
  -- state — it is what runs whenever no model is configured.
  origin            TEXT NOT NULL CHECK (origin IN ('model', 'fallback', 'remediation')),
  provider          TEXT,
  model             TEXT,
  -- The rubric version that produced it. Improving the prompt is a deliberate act: bump
  -- this and you can find and regenerate exactly the cohort the old one wrote, rather
  -- than leaving a mixed population with no way to tell which is which. The same
  -- discipline question_explanations already uses.
  prompt_version    INTEGER NOT NULL,
  -- Accuracy over the scope when the plan was written. Staleness is measured against it.
  accuracy_at_generation NUMERIC,

  status            TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'completed', 'archived')),
  -- The one line shown on the plan card. Stored rather than recomputed: it was written
  -- by whichever planner produced this plan, and a plan whose summary drifts from its
  -- steps as the rules change is worse than one that reads slightly dated.
  summary           TEXT NOT NULL DEFAULT '',
  -- Which subject the card wears the colours of. Denormalised like scope_title: a
  -- subtopic carries no subject of its own, so resolving it means walking to the
  -- chapter, and the history screen should not need a tree walk per row.
  subject           TEXT,
  completed_at      TIMESTAMPTZ,
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- For anyone who applied the table before the column existed. Both paths land in the
-- same place, which is what this file being the migration mechanism requires.
ALTER TABLE study_plans ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT '';
ALTER TABLE study_plans ADD COLUMN IF NOT EXISTS subject TEXT;

CREATE INDEX IF NOT EXISTS idx_study_plans_user
  ON study_plans (firebase_uid, status, updated_at DESC);

-- Asking for the same scope twice resumes rather than forks. Without this a student who
-- taps "Plan this" again gets a second plan, and their finished steps appear to vanish.
CREATE UNIQUE INDEX IF NOT EXISTS uq_study_plans_active_scope
  ON study_plans (firebase_uid, scope_node_id) WHERE status = 'active';


-- One step of a plan. Ordered by `position` and rendered as a rail, but stored as a DAG:
-- `depends_on` costs nothing now and means the graph view later is a rendering change
-- rather than a migration. Foundation steps really are a branch that rejoins, so the DAG
-- is also the more honest shape.
CREATE TABLE IF NOT EXISTS study_plan_steps (
  step_id        UUID PRIMARY KEY,
  plan_id        UUID NOT NULL REFERENCES study_plans(plan_id) ON DELETE CASCADE,
  position       INTEGER NOT NULL,
  kind           TEXT NOT NULL
                 CHECK (kind IN ('learn', 'practise', 'verify', 'consolidate')),

  title          TEXT NOT NULL,
  why            TEXT NOT NULL,
  -- The instructions. This is the feature: a step that names material without saying how
  -- to use it is a link, and the student already had links.
  how_to_use     TEXT[] NOT NULL,
  focus_node_ids TEXT[] NOT NULL DEFAULT '{}',
  is_foundation  BOOLEAN NOT NULL DEFAULT false,
  depends_on     UUID[] NOT NULL DEFAULT '{}',
  estimated_minutes INTEGER,

  -- How the step is judged done. 'self' is the only one a tap can satisfy, and it is
  -- allowed only where there is genuinely nothing to measure — reading and watching.
  -- Everything else is derived from the attempt log in JM-3.
  completion_kind    TEXT NOT NULL
                     CHECK (completion_kind IN ('self', 'accuracy', 'checkpoint')),
  required_questions INTEGER,
  required_accuracy  NUMERIC,
  CONSTRAINT graded_steps_state_their_bar CHECK (
    completion_kind = 'self'
    OR (required_questions IS NOT NULL AND required_accuracy IS NOT NULL)
  ),

  state          TEXT NOT NULL DEFAULT 'pending'
                 CHECK (state IN ('pending', 'in_progress', 'done', 'skipped')),
  completed_at   TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),

  UNIQUE (plan_id, position)
);

CREATE INDEX IF NOT EXISTS idx_plan_steps_plan ON study_plan_steps (plan_id, position);


-- One item inside a step: either a named reference or a question selector, never both.
--
-- That split is the content boundary showing through into the schema. Videos and notes
-- are few and individually meaningful, so a plan names them. Questions are many and
-- interchangeable, so a plan describes a filter and the backend resolves it — which is
-- why no column here can hold a question id the planner chose.
CREATE TABLE IF NOT EXISTS study_plan_step_items (
  item_id      UUID PRIMARY KEY,
  step_id      UUID NOT NULL REFERENCES study_plan_steps(step_id) ON DELETE CASCADE,
  position     INTEGER NOT NULL,
  item_type    TEXT NOT NULL
               CHECK (item_type IN ('video', 'notes', 'test', 'questions')),

  ref_node_id  TEXT,
  ref_id       TEXT,

  sel_concept_ids  TEXT[],
  sel_types        TEXT[],
  sel_difficulty   TEXT[],
  sel_count        INTEGER,
  sel_order        TEXT CHECK (sel_order IN ('easiest_first', 'mixed', 'hardest_first')),
  sel_exclude_seen BOOLEAN NOT NULL DEFAULT false,

  -- Frozen on first open (JM-3). The selector is the plan; this is the sitting. Without
  -- it the deck reshuffles between visits and "6 of 8 right" stops meaning anything.
  resolved_question_ids TEXT[],
  resolved_at  TIMESTAMPTZ,

  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT item_is_a_reference_or_a_selector CHECK (
    (item_type = 'questions' AND sel_count IS NOT NULL AND ref_id IS NULL)
    OR (item_type <> 'questions' AND ref_id IS NOT NULL AND sel_count IS NULL)
  ),

  UNIQUE (step_id, position)
);

CREATE INDEX IF NOT EXISTS idx_plan_items_step ON study_plan_step_items (step_id, position);


-- How long a lecture runs. Without it a step cannot say "watch to 6:40" and cannot
-- estimate honestly, so the planner has been writing duration-free guidance. Nullable
-- and backfilled through the admin path; the inventory reads it once it is populated.
-- Every attempt to generate a plan, whether or not one came out of it.
--
-- The spend limits used to be counted over `study_plans`, which had two problems. The
-- small one: a generation that failed still cost money and left no trace, so the limit
-- undercounted exactly when it mattered. The large one: counting rows that do not exist
-- yet cannot be atomic — two requests both counted two, both saw room, and both paid.
-- Six concurrent requests produced six plans against a cap of three.
--
-- So a row is written here *before* the model is called, under a per-student lock, and
-- the limit is a count of these. A reservation that never becomes a plan still counts,
-- which is the correct answer to "what has this student spent".
CREATE TABLE IF NOT EXISTS plan_generations (
  generation_id UUID PRIMARY KEY,
  firebase_uid  TEXT NOT NULL REFERENCES users(firebase_uid) ON DELETE CASCADE,
  tenant_id     TEXT NOT NULL REFERENCES tenants(tenant_id),
  scope_node_id TEXT NOT NULL,
  -- Set when the attempt resolves. `started` rows that never resolve are a crash during
  -- generation; they still count, because the call was still made.
  outcome       TEXT NOT NULL DEFAULT 'started'
                CHECK (outcome IN ('started', 'saved', 'failed')),
  plan_id       UUID REFERENCES study_plans(plan_id) ON DELETE SET NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_plan_generations_recent
  ON plan_generations (firebase_uid, created_at DESC);


-- Which round of remediation added a step. Zero for everything the planner wrote; one
-- for the work appended after the first missed checkpoint, and so on. Stored rather than
-- inferred because it is the only thing bounding how far a plan can grow — and because a
-- student is owed the difference between "this was always the plan" and "this appeared
-- because of your check".
ALTER TABLE study_plan_steps
  ADD COLUMN IF NOT EXISTS remediation_round INTEGER NOT NULL DEFAULT 0;

ALTER TABLE node_videos ADD COLUMN IF NOT EXISTS duration_seconds INTEGER;


-- ---------------------------------------------------------------------------
-- Billing. Three tables and one cache column.
--
-- Deliberately *not* called "plans": `study_plans` already means a Jeene Mode plan, and
-- one word meaning two things poisons every query and every conversation after it. The
-- sellable things are products.
-- ---------------------------------------------------------------------------

-- What an admin can sell. Rows are retired, never deleted: a payment made last month
-- still points here, and a deleted product would orphan somebody's receipt.
CREATE TABLE IF NOT EXISTS products (
  product_id     TEXT PRIMARY KEY,
  tenant_id      TEXT NOT NULL REFERENCES tenants(tenant_id),
  title          TEXT NOT NULL,
  tier           TEXT NOT NULL DEFAULT 'pro',
  -- Integer minor units, always. `1199.995 * 100` is 119999.49999999999 on one runtime
  -- and 119999.5 on another, and once that is true, comparing two amounts for equality
  -- stops being a yes-or-no question. Paise never has that problem.
  amount_paise   BIGINT NOT NULL CHECK (amount_paise > 0),
  currency       TEXT NOT NULL DEFAULT 'INR',
  duration_days  INTEGER NOT NULL CHECK (duration_days > 0),
  -- "Most Popular", "Save 16%". Decoration for the card; never read by arithmetic.
  badge          TEXT NOT NULL DEFAULT '',
  sort_order     INTEGER NOT NULL DEFAULT 0,
  active         BOOLEAN NOT NULL DEFAULT TRUE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_products_sellable
  ON products (tenant_id, sort_order) WHERE active;


-- One row per attempt to pay. Keyed by Razorpay's own order id so the two systems can
-- never disagree about which record is which.
CREATE TABLE IF NOT EXISTS payments (
  order_id            TEXT PRIMARY KEY,
  firebase_uid        TEXT NOT NULL REFERENCES users(firebase_uid) ON DELETE RESTRICT,
  tenant_id           TEXT NOT NULL REFERENCES tenants(tenant_id),
  product_id          TEXT NOT NULL REFERENCES products(product_id),

  -- Copied from the product at creation and never re-read. A price change tomorrow must
  -- not rewrite what somebody paid yesterday, and a receipt has to still make sense when
  -- the product it names has been retired.
  amount_paise        BIGINT NOT NULL CHECK (amount_paise > 0),
  currency            TEXT NOT NULL,
  duration_days       INTEGER NOT NULL CHECK (duration_days > 0),

  status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','paid','failed','needs_manual_review')),
  razorpay_payment_id TEXT,
  failure_reason      TEXT NOT NULL DEFAULT '',
  -- What the gateway actually said, verbatim, for the day somebody disputes a charge.
  gateway_payload     JSONB NOT NULL DEFAULT '{}'::jsonb,

  created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
  settled_at          TIMESTAMPTZ,
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The reconciler's working set: unsettled rows, oldest first.
CREATE INDEX IF NOT EXISTS idx_payments_pending
  ON payments (created_at) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_payments_user
  ON payments (firebase_uid, created_at DESC);

-- One captured payment backs exactly one order. If a duplicate webhook ever tries to
-- attach the same capture to a second order, the database refuses rather than the code
-- remembering to.
CREATE UNIQUE INDEX IF NOT EXISTS idx_payments_capture
  ON payments (razorpay_payment_id) WHERE razorpay_payment_id IS NOT NULL;


-- Why access is granted, append-only.
--
-- Expiry is derived by summing this table, never stored as the truth. With a single
-- mutable expiry column, a webhook delivered twice adds thirty days twice and nobody
-- finds out until a student writes in. Here the second write is refused by
-- `idx_grant_per_order` below, which is the one constraint that makes all four
-- confirmation paths safe to fire more than once.
--
-- A refund subtracts by inserting a negative `days` row. History is added to, never
-- edited, so "why is this account Pro until March" is always answerable.
CREATE TABLE IF NOT EXISTS entitlement_grants (
  grant_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firebase_uid  TEXT NOT NULL REFERENCES users(firebase_uid) ON DELETE CASCADE,
  tenant_id     TEXT NOT NULL REFERENCES tenants(tenant_id),
  tier          TEXT NOT NULL DEFAULT 'pro',
  days          INTEGER NOT NULL,
  -- 'razorpay' today; 'apple' or 'manual' later without a migration. This column is the
  -- seam that keeps a change of store from becoming a redesign of entitlements.
  provider      TEXT NOT NULL,
  -- Null for a comp or a support grant; set for anything paid.
  order_id      TEXT REFERENCES payments(order_id),
  note          TEXT NOT NULL DEFAULT '',
  granted_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_grant_per_order
  ON entitlement_grants (order_id) WHERE order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_grants_user
  ON entitlement_grants (firebase_uid, tenant_id, tier);


-- A cache of the sum above, written inside the same transaction as the grant. The ledger
-- stays the truth; this exists so the gate on a practice request is one indexed read
-- rather than an aggregate. Null means "never had access".
ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_expires_at TIMESTAMPTZ;


-- =====================================================================================
-- Ask Jeene — doubts asked while studying
-- =====================================================================================
--
-- A student reading a chapter asks a question and is answered out of material this app
-- already owns: the concept descriptions, the worked solutions, and the chapter's notes.
-- What is stored here is the conversation and, for every answer, exactly what it was
-- built from.
--
-- That last part is not bookkeeping. It is how an answer can be checked rather than
-- trusted — an id cited here that was never supplied to the model means it went outside
-- its material — and with under-18 students and a generative feature, being able to
-- answer "what did it say to this child" is not optional.

-- One thread per student per chapter.
--
-- Not per question and not per session: a student working through Gravitation asks about
-- question 14, then about the concept behind it, then about question 17, and that is one
-- conversation. The anchor lives on each message instead, so the thread follows the
-- chapter while every turn remembers exactly what was on screen.
CREATE TABLE IF NOT EXISTS doubt_threads (
  thread_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  firebase_uid    TEXT NOT NULL REFERENCES users(firebase_uid) ON DELETE CASCADE,
  tenant_id       TEXT NOT NULL REFERENCES tenants(tenant_id),
  chapter_id      TEXT NOT NULL REFERENCES nodes(node_id),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_message_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The "one per student per chapter" rule, enforced rather than remembered: opening the
-- same chapter twice must continue the conversation, not start a second one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_doubt_thread_per_chapter
  ON doubt_threads (firebase_uid, chapter_id);
CREATE INDEX IF NOT EXISTS idx_doubt_threads_recent
  ON doubt_threads (firebase_uid, last_message_at DESC);


CREATE TABLE IF NOT EXISTS doubt_messages (
  message_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  thread_id         UUID NOT NULL REFERENCES doubt_threads(thread_id) ON DELETE CASCADE,
  -- Denormalised from the thread, the same way `attempts.tenant_id` is, so the daily
  -- allowance is one indexed read rather than a join on the hottest check in the feature.
  firebase_uid      TEXT NOT NULL REFERENCES users(firebase_uid) ON DELETE CASCADE,
  role              TEXT NOT NULL CHECK (role IN ('student', 'jeene')),
  text              TEXT NOT NULL,

  -- What was on screen when they asked. Null on Jeene's own messages.
  anchor_kind       TEXT CHECK (anchor_kind IN ('question', 'notes', 'node')),
  anchor_id         TEXT,

  -- What the answer was actually built from. Every id here was handed to the model; one
  -- that was not is the signal that it answered from memory instead of from the material.
  used_concept_ids  TEXT[] NOT NULL DEFAULT '{}',
  used_question_ids TEXT[] NOT NULL DEFAULT '{}',
  used_notes        BOOLEAN NOT NULL DEFAULT FALSE,
  -- False when the material did not settle the question and Jeene said so. Watching this
  -- rate is how you tell a doubt solver that is inventing from one that is too thin:
  -- near zero means the first, very high means the second.
  answered          BOOLEAN,

  -- What it cost, so the real figure replaces an estimate after a week of use.
  model             TEXT,
  tokens_in         INTEGER,
  tokens_out        INTEGER,

  -- A student saying this was wrong. The best bug reports in the feature will come from
  -- here, and there is nowhere else they could come from.
  reported          BOOLEAN NOT NULL DEFAULT FALSE,

  created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_doubt_messages_thread
  ON doubt_messages (thread_id, created_at);
-- The daily allowance: a student's own messages, over a rolling day.
CREATE INDEX IF NOT EXISTS idx_doubt_messages_allowance
  ON doubt_messages (firebase_uid, created_at DESC) WHERE role = 'student';
