#!/bin/sh
# Ask the backend to sweep every payment it is still unsure about.
#
# Run by Render's cron every five minutes. It is a safety net, not the main path: three
# other things confirm a payment first, and this one exists for when none of them arrive
# — a phone that died on the success screen, a webhook dropped during a deploy.
#
# Idempotent, so a missed run costs nothing but a few minutes' delay for one student, and
# a run that happens twice does nothing the first did not.
#
# Exits non-zero on anything but 200, so a broken secret or a sleeping service shows up as
# a failed cron run in the dashboard rather than as silence.
set -eu

: "${JEENE_API_BASE:?JEENE_API_BASE is not set}"
: "${JEENE_RECONCILE_SECRET:?JEENE_RECONCILE_SECRET is not set}"

body=$(mktemp)
trap 'rm -f "$body"' EXIT

code=$(curl --silent --show-error --max-time 90 \
            --output "$body" --write-out '%{http_code}' \
            --request POST "${JEENE_API_BASE}/internal/billing/reconcile" \
            --header "X-Jeene-Reconcile: ${JEENE_RECONCILE_SECRET}")

cat "$body"
echo

if [ "$code" != "200" ]; then
    echo "reconcile failed: HTTP $code" >&2
    exit 1
fi
