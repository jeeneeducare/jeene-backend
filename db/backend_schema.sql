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
-- Which round of remediation added a step. Zero for everything the planner wrote; one
-- for the work appended after the first missed checkpoint, and so on. Stored rather than
-- inferred because it is the only thing bounding how far a plan can grow — and because a
-- student is owed the difference between "this was always the plan" and "this appeared
-- because of your check".
ALTER TABLE study_plan_steps
  ADD COLUMN IF NOT EXISTS remediation_round INTEGER NOT NULL DEFAULT 0;

ALTER TABLE node_videos ADD COLUMN IF NOT EXISTS duration_seconds INTEGER;
