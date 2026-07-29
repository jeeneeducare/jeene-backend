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
