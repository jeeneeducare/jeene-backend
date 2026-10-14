"""The sweeps nobody triggers. Cron reaches these; no student ever does.

Two endpoints live here: the purchase reconciler, the last of the confirmation paths. The
other two depend on something arriving — the app handing a purchase over, a store's
notification — and each has a way of not arriving. A phone dies between the store's
sheet and its next request. A notification is dropped, or lands while a deploy is
restarting. Google refunds a purchase nobody consumed within three days.

The reconciler assumes none of that arrived. It asks the stores directly, which makes it
the path that closes a purchase when every other one has failed, and the reason a student
who paid always ends up with what they paid for even when the network did not cooperate.

The second is Ask Jeene's health, which grants nothing and is here because it wants the
same lock and the same caller: something watching, on a schedule, with no user.

Authenticated by a shared secret in a header, not by a user token: cron has no user. A
deployment that has not been given the secret serves 503 here rather than running the
sweep unauthenticated, because the reconciler can grant access.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException

from app.billing import BillingUnavailable, app_store, google_play, purchases
from app.billing.notifications import handle_apple
from app.billing.stores import (
    GOOGLE,
    AppStore,
    GooglePlay,
    ProofRejected,
    StoreUnreachable,
)
from app.config import settings
from app.db import get_connection
from app.doubts import answer as answer_module
from app.schemas import DoubtHealth, ReconcileReport
from app.security import constant_time_equals

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

#: How long a purchase is left to its own paths before the sweep touches it. The request
#: that granted a Google purchase consumes it a moment later, and racing it would only
#: produce a second consume for Google to refuse.
_GRACE_SECONDS = 120

#: How far back refunds and Apple's undelivered notifications are read. Every step is
#: idempotent, so reading the same three days every five minutes costs a request and
#: changes nothing twice; the width is there so an outage of a day or two is still caught.
_LOOKBACK = timedelta(days=3)

#: One sweep's worth of rows per kind. Each costs a call to Google, and this holds a
#: pooled connection while it runs; a backlog is finished by the next run.
_BATCH = 25

#: Wall-clock budget, so a store having a slow day cannot pin a connection for the full
#: batch × timeout. Whatever is left is simply swept next time.
_BUDGET_SECONDS = 45.0


def _authorise(presented: str) -> None:
    """Constant-time check of the cron's shared secret.

    Unset secret is a refusal, not a bypass. The alternative — an endpoint that grants
    entitlements and is open whenever an environment variable is missing — is the kind of
    default that survives right up until the day it does not.
    """
    expected = settings.jeene_reconcile_secret
    if not expected:
        logger.error("reconcile called but JEENE_RECONCILE_SECRET is not set")
        raise HTTPException(status_code=503, detail="unavailable")
    if not presented or not constant_time_equals(presented, expected):
        raise HTTPException(status_code=403, detail="forbidden")


def _configured(build) -> object | None:
    try:
        return build()
    except BillingUnavailable:
        return None


@router.post("/billing/reconcile", response_model=ReconcileReport, include_in_schema=False)
async def reconcile(
    x_jeene_reconcile: str = Header(default=""),
    connection: asyncpg.Connection = Depends(get_connection),
) -> ReconcileReport:
    """Ask the stores about everything still unsettled, and catch what was missed.

    **Google**, three things:

      * granted purchases nobody has consumed yet are consumed — before Google's
        three-day automatic refund of anything left unacknowledged;
      * pending purchases are read again, and granted or cancelled as Google now says;
      * the last three days of voided purchases are applied, for any refund whose
        notification never arrived.

    **Apple**, one: the notifications Apple could not deliver in the last three days are
    read back from its history and handled exactly as the route would have.

    A store that is not configured is skipped, not an error — one store can go live
    before the other.
    """
    _authorise(x_jeene_reconcile)

    # Billing switched off is a *configuration*, not a fault. Answering it with an error
    # made the cron fail every five minutes from the moment it was created until billing
    # was turned on — which teaches whoever set it up to ignore a red run on the one job
    # whose whole purpose is to be trusted when it complains. And switching billing off is
    # the lever to pull when something is wrong at eleven at night; pulling it must not
    # start an alarm storm on top of whatever was already going on.
    if not settings.jeene_billing_enabled:
        logger.info("reconcile: billing is switched off; nothing to sweep")
        return ReconcileReport(status="disabled")

    google = _configured(google_play)
    apple = _configured(app_store)
    # Switched *on* with neither store behind it is a different thing entirely: a
    # deployment that believes it is selling and cannot confirm a purchase. That still
    # fails loudly, because somebody has to find out.
    if google is None and apple is None:
        logger.error("reconcile: billing is enabled but no store is configured")
        raise HTTPException(status_code=503, detail="Payments are not configured")

    report = ReconcileReport()
    started = time.monotonic()
    if google is not None:
        await _sweep_google(connection, google, report, started)
    if apple is not None:
        await _sweep_apple(connection, apple, report, started)
    return report


def _out_of_time(started: float) -> bool:
    return time.monotonic() - started > _BUDGET_SECONDS


async def _sweep_google(
    connection: asyncpg.Connection, client: GooglePlay, report: ReconcileReport, started: float
) -> None:
    unconsumed = await connection.fetch(
        """
        SELECT store_ref, store_product_id FROM store_purchases
         WHERE store = 'google' AND status = 'granted' AND consumed_at IS NULL
           AND created_at < now() - make_interval(secs => $1)
         ORDER BY created_at
         LIMIT $2
        """,
        _GRACE_SECONDS, _BATCH,
    )
    for row in unconsumed:
        if _out_of_time(started):
            return
        try:
            await client.consume(row["store_product_id"], row["store_ref"])
        except (StoreUnreachable, ProofRejected):
            logger.warning("reconcile could not consume %s", row["store_ref"][:12])
            report.errors += 1
            continue
        await purchases.mark_consumed(connection, row["store_ref"])
        report.consumed += 1

    pending = await connection.fetch(
        """
        SELECT store_ref FROM store_purchases
         WHERE store = 'google' AND status = 'pending'
           AND created_at < now() - make_interval(secs => $1)
         ORDER BY updated_at
         LIMIT $2
        """,
        _GRACE_SECONDS, _BATCH,
    )
    for row in pending:
        if _out_of_time(started):
            return
        try:
            purchase = await client.get_purchase(row["store_ref"])
            owner = await purchases.owner_of(connection, purchase, handed_over_by=None)
            if owner is None:  # pragma: no cover — a recorded purchase always has one
                continue
            outcome = await purchases.record(connection, purchase, owner)
        except (StoreUnreachable, ProofRejected, purchases.UnknownAccount,
                purchases.NotForSale) as exc:
            logger.warning("reconcile could not settle a pending purchase: %s", exc)
            report.errors += 1
            continue
        if outcome.status == purchases.GRANTED:
            report.granted += 1
            await purchases.consume_if_due(connection, client, purchase, outcome)
        elif outcome.status == purchases.STATUS_CANCELLED:
            report.cancelled += 1
        else:
            report.left_pending += 1

    if _out_of_time(started):
        return
    try:
        voided = await client.voided_since(datetime.now(timezone.utc) - _LOOKBACK)
    except (StoreUnreachable, ProofRejected) as exc:
        logger.warning("reconcile could not read Google's voided purchases: %s", exc)
        report.errors += 1
        return
    for item in voided:
        if await purchases.void(connection, GOOGLE, item.purchase_token,
                                voided_at=item.voided_at):
            report.refunded += 1


async def _sweep_apple(
    connection: asyncpg.Connection, store: AppStore, report: ReconcileReport, started: float
) -> None:
    if _out_of_time(started):
        return
    try:
        payloads = await store.notification_history(datetime.now(timezone.utc) - _LOOKBACK)
    except StoreUnreachable as exc:
        logger.warning("reconcile could not read Apple's notification history: %s", exc)
        report.errors += 1
        return
    for signed in payloads:
        if _out_of_time(started):
            return
        try:
            note = await store.verify_notification(signed)
        except (StoreUnreachable, ProofRejected) as exc:
            logger.warning("reconcile could not verify an Apple notification: %s", exc)
            report.errors += 1
            continue
        result = await handle_apple(connection, note)
        if result == purchases.GRANTED:
            report.granted += 1
        elif result == "refunded":
            report.refunded += 1

_HEALTH_SQL = """
SELECT
  count(*) FILTER (WHERE role = 'student')                          AS asked,
  count(*) FILTER (WHERE role = 'jeene' AND answered)               AS answered,
  count(*) FILTER (WHERE role = 'jeene' AND NOT answered)           AS declined,
  count(*) FILTER (WHERE role = 'jeene' AND reported)               AS reported,
  count(DISTINCT firebase_uid)                                      AS students,
  coalesce(sum(tokens_in), 0)                                       AS tokens_in,
  coalesce(sum(tokens_out), 0)                                      AS tokens_out
  FROM doubt_messages
 WHERE created_at > now() - make_interval(days => $1)
"""

#: An answer the citation check threw away is recorded as declined *and* carries the
#: refusal copy, which is how it is told apart from a refusal the model chose to make.
_UNGROUNDED_SQL = """
SELECT count(*) FROM doubt_messages
 WHERE role = 'jeene' AND NOT answered AND text = $2
   AND created_at > now() - make_interval(days => $1)
"""

_BUSIEST_SQL = """
SELECT n.title, count(*) AS asked
  FROM doubt_messages m
  JOIN doubt_threads t ON t.thread_id = m.thread_id
  JOIN nodes n ON n.node_id = t.chapter_id
 WHERE m.role = 'student' AND m.created_at > now() - make_interval(days => $1)
 GROUP BY n.title ORDER BY asked DESC LIMIT 5
"""


@router.get("/doubts/health", response_model=DoubtHealth, include_in_schema=False)
async def doubts_health(
    days: int = 7,
    x_jeene_reconcile: str = Header(default=""),
    connection: asyncpg.Connection = Depends(get_connection),
) -> DoubtHealth:
    """How Ask Jeene has been doing, over the last `days`.

    Read-only, and behind the cron secret rather than open: what a student asked is their
    conversation, and while nothing here returns the text of one, the shape of somebody's
    week is not public either.

    The number to watch is `refusal_rate`. Near zero means it is answering past what the
    material supports; very high means the material is too thin to be worth asking. What
    *must* stay at zero is `ungrounded` — those are answers thrown away for citing
    material they were never given, and a rising count is the prompt or the material block
    having drifted, not a student asking something awkward.
    """
    _authorise(x_jeene_reconcile)
    days = max(1, min(days, 90))

    row = await connection.fetchrow(_HEALTH_SQL, days)
    ungrounded = await connection.fetchval(
        _UNGROUNDED_SQL, days, answer_module.COULD_NOT_ANSWER
    )
    busiest = await connection.fetch(_BUSIEST_SQL, days)

    replied = row["answered"] + row["declined"]
    return DoubtHealth(
        days=days,
        asked=row["asked"],
        answered=row["answered"],
        declined=row["declined"],
        refusal_rate=round(row["declined"] / replied, 3) if replied else 0.0,
        ungrounded=ungrounded or 0,
        reported=row["reported"],
        students=row["students"],
        tokens_in=row["tokens_in"],
        tokens_out=row["tokens_out"],
        busiest_chapters=[f"{r['title']} ({r['asked']})" for r in busiest],
    )
