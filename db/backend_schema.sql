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
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_login_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_users_tenant ON users (tenant_id);
