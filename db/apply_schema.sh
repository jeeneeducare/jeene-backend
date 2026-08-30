#!/usr/bin/env bash
#
# Apply db/backend_schema.sql to a database, safely and visibly.
#
# The file is written to be re-runnable — every table is CREATE TABLE IF NOT EXISTS and
# every column is ADD COLUMN IF NOT EXISTS, and it contains no DROP, TRUNCATE, DELETE or
# ALTER COLUMN. Running it against a live database adds what is missing and leaves
# everything else alone. This script exists to *show* you that before it happens.
#
# The connection string is read from a prompt rather than an argument, so it does not end
# up in your shell history, and it is never echoed.
#
#   ./db/apply_schema.sh
#
set -euo pipefail

cd "$(dirname "$0")/.."
SCHEMA="db/backend_schema.sql"

command -v psql >/dev/null || {
  echo "psql not found. On a Mac: brew install postgresql@17" >&2
  exit 1
}
[ -f "$SCHEMA" ] || { echo "Cannot find $SCHEMA" >&2; exit 1; }

printf 'Connection string (Supabase > Connect > Session pooler): '
read -rs DATABASE_URL
printf '\n\n'
[ -n "$DATABASE_URL" ] || { echo "Nothing entered." >&2; exit 1; }
export PGCONNECT_TIMEOUT=15

# Everything Jeene Mode needs. Checked before and after, so the run is verifiable rather
# than merely quiet.
read -r -d '' CHECK <<'SQL' || true
SELECT format('  %-34s %s', obj, CASE WHEN present THEN 'present' ELSE 'MISSING' END)
FROM (
  SELECT 'table study_plans'            AS obj, to_regclass('public.study_plans')            IS NOT NULL AS present
  UNION ALL SELECT 'table study_plan_steps',      to_regclass('public.study_plan_steps')      IS NOT NULL
  UNION ALL SELECT 'table study_plan_step_items', to_regclass('public.study_plan_step_items') IS NOT NULL
  UNION ALL SELECT 'table plan_generations',      to_regclass('public.plan_generations')      IS NOT NULL
  UNION ALL SELECT 'table chapter_notes',         to_regclass('public.chapter_notes')         IS NOT NULL
  UNION ALL SELECT 'column remediation_round', EXISTS (
      SELECT 1 FROM information_schema.columns
       WHERE table_name = 'study_plan_steps' AND column_name = 'remediation_round')
  UNION ALL SELECT 'column duration_seconds', EXISTS (
      SELECT 1 FROM information_schema.columns
       WHERE table_name = 'node_videos' AND column_name = 'duration_seconds')
  UNION ALL SELECT 'index uq_study_plans_active_scope',
      to_regclass('public.uq_study_plans_active_scope') IS NOT NULL
) t ORDER BY obj;
SQL

echo "Connecting…"
psql "$DATABASE_URL" -tAc "SELECT 'Connected to ' || current_database() || ' as ' || current_user"

echo
echo "Before:"
psql "$DATABASE_URL" -tAc "$CHECK"

echo
echo "This will run $SCHEMA. It creates what is missing above and changes nothing else:"
echo "  no DROP, no TRUNCATE, no DELETE, no ALTER COLUMN — additive only."
printf 'Type yes to continue: '
read -r CONFIRM
[ "$CONFIRM" = "yes" ] || { echo "Stopped. Nothing was changed."; exit 0; }

echo
echo "Applying…"
# ON_ERROR_STOP so a failure stops at the first problem instead of half-applying.
# "already exists, skipping" notices are the expected output on a database that has
# most of this; they are not errors.
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f "$SCHEMA" 2>&1 \
  | grep -viE "(already exists|does not exist), skipping" \
  | grep -vE "^(CREATE|ALTER|COMMENT|INSERT|SET)" || true

echo
echo "After:"
psql "$DATABASE_URL" -tAc "$CHECK"

if psql "$DATABASE_URL" -tAc "$CHECK" | grep -q MISSING; then
  echo
  echo "Something is still missing. Do not push the backend until this is clean." >&2
  exit 1
fi

echo
echo "All present. The backend can be pushed now."
