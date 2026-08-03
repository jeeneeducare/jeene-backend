#!/usr/bin/env bash
#
# Give a Google account access to the admin panel, or take it away.
#
# Access is a row in `admins`, keyed by Firebase uid. The email is stored alongside it
# for the sake of whoever reads the table later, but it is not what the check uses: an
# email can be changed on a Google account and a uid cannot, so the uid is the identity
# and the email is a label.
#
# The uid only exists once that person has signed in to something Firebase at least once,
# which for the admin panel means opening it and being told they are not an administrator.
# That refusal is the step that creates them. So the order is: they sign in, get refused,
# you run this, they reload.
#
# Usage:
#   ./grant_admin.sh someone@gmail.com                 # grant
#   ./grant_admin.sh someone@gmail.com --note "content" # grant, with a reason
#   ./grant_admin.sh someone@gmail.com --revoke        # take it away
#   ./grant_admin.sh --list
#
# Needs: JEENE_POSTGRES_URL, and gcloud authenticated as the Firebase service account
#   gcloud auth activate-service-account --key-file=~/.jeene-firebase-sa.json

set -euo pipefail

PROJECT="jeene-ff8e3"
TENANT="${JEENE_TENANT:-JEENE_MASTER}"

if [[ -z "${JEENE_POSTGRES_URL:-}" ]]; then
  echo "set JEENE_POSTGRES_URL" >&2; exit 1
fi

if [[ "${1:-}" == "--list" ]]; then
  psql "$JEENE_POSTGRES_URL" -c \
    "SELECT email, tenant_id, note, added_at FROM admins ORDER BY added_at;"
  exit 0
fi

EMAIL="${1:-}"
if [[ -z "$EMAIL" ]]; then
  echo "usage: $0 <email> [--note \"why\"] [--revoke]   |   $0 --list" >&2; exit 1
fi

NOTE=""
REVOKE=false
shift
while [[ $# -gt 0 ]]; do
  case "$1" in
    --note) NOTE="${2:-}"; shift 2 ;;
    --revoke) REVOKE=true; shift ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

if $REVOKE; then
  psql "$JEENE_POSTGRES_URL" -v ON_ERROR_STOP=1 -c \
    "DELETE FROM admins WHERE lower(email) = lower('${EMAIL//\'/\'\'}');"
  echo "Revoked $EMAIL. It takes effect on their next request; a page already open keeps"
  echo "working until it asks the server for something, which is within a click or two."
  exit 0
fi

TOKEN="$(gcloud auth print-access-token 2>/dev/null || true)"
if [[ -z "$TOKEN" ]]; then
  echo "gcloud is not authenticated. Run:" >&2
  echo "  gcloud auth activate-service-account --key-file=~/.jeene-firebase-sa.json" >&2
  exit 1
fi

# Ask Firebase who owns this email. This is the only place the uid can come from, and it
# is why the person has to have signed in once before this will work.
UID_FOUND="$(curl -fsS -X POST \
  "https://identitytoolkit.googleapis.com/v1/projects/${PROJECT}/accounts:lookup" \
  -H "Authorization: Bearer ${TOKEN}" -H 'Content-Type: application/json' \
  -d "{\"email\":[\"${EMAIL}\"]}" \
  | python3 -c 'import json,sys; u=json.load(sys.stdin).get("users",[]); print(u[0]["localId"] if u else "")')"

if [[ -z "$UID_FOUND" ]]; then
  cat >&2 <<MSG
No Firebase account for ${EMAIL} yet.

They need to open https://jeene-admin.web.app and sign in with Google once. They will be
told they are not an administrator, which is correct and is the step that creates the
account. Run this again afterwards.
MSG
  exit 1
fi

psql "$JEENE_POSTGRES_URL" -v ON_ERROR_STOP=1 <<SQL
INSERT INTO admins (firebase_uid, tenant_id, email, note)
VALUES ('${UID_FOUND}', '${TENANT}', '${EMAIL//\'/\'\'}', '${NOTE//\'/\'\'}')
ON CONFLICT (firebase_uid) DO UPDATE
  SET email = EXCLUDED.email, tenant_id = EXCLUDED.tenant_id, note = EXCLUDED.note;
SQL

echo "Granted ${EMAIL} (${UID_FOUND}) on ${TENANT}."
echo "They reload https://jeene-admin.web.app and they are in."
