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
