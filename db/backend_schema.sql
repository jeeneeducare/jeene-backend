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

  -- Short, human-typeable handle for moving this sitting to a browser.
  handoff_code  TEXT UNIQUE,

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

  -- Per-question review flags. These are presentation state, not answers, so they
  -- live here rather than polluting the attempt log.
  marked_for_review TEXT[] NOT NULL DEFAULT '{}',

  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_test_sessions_user ON test_sessions (firebase_uid, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_test_sessions_test ON test_sessions (test_id);
