"""The sweeps nobody triggers. Cron reaches these; no student ever does.

Two endpoints live here: the payment reconciler, the fourth and last of the
confirmation paths. The other three all depend on something arriving — a client callback,
a webhook, a failure report — and each has a way of not arriving. A phone dies on the
success screen. A webhook is dropped, or lands while a deploy is restarting. A student
force-quits mid-capture.

The reconciler assumes none of that. It asks Razorpay directly about every payment still
sitting at `pending`, which makes it the path that closes a purchase when every other one
has failed, and the reason a student who paid always ends up with what they paid for
even when the network did not cooperate.

The second is Ask Jeene's health, which grants nothing and is here because it wants the
same lock and the same caller: something watching, on a schedule, with no user.

Authenticated by a shared secret in a header, not by a user token: cron has no user. A
deployment that has not been given the secret serves 503 here rather than running the
sweep unauthenticated, because the reconciler can grant access.
"""

from __future__ import annotations

import logging
import time

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException

from app.billing import BillingUnavailable, gateway, settle
from app.billing.gateway import constant_time_equals
from app.config import settings
from app.db import get_connection
from app.doubts import answer as answer_module
from app.schemas import DoubtHealth, ReconcileReport

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])

#: How long a payment is left alone before it is asked about. A checkout that is going to
#: succeed usually has, and its own three confirmation paths get first refusal; sweeping
#: at ten seconds would mean racing the student's own success callback for no gain.
_GRACE_SECONDS = 300

#: When a pending payment stops being "in flight" and starts being abandoned. Razorpay
#: keeps an unpaid order open far longer than this, but a student who walked away half an
#: hour ago should not be looking at "payment in progress" — they should be able to try
#: again. Nothing is failed at this point without asking the gateway first.
_GIVE_UP_SECONDS = 1800

#: One sweep's worth. Each row costs a call to Razorpay, and this holds a pooled
#: connection while it runs; a backlog is finished by the next run five minutes later
#: rather than by one run that monopolises a connection for a minute.
_BATCH = 25

#: Wall-clock budget, so a gateway having a slow day cannot pin a connection for the full
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


@router.post("/billing/reconcile", response_model=ReconcileReport, include_in_schema=False)
async def reconcile(
    x_jeene_reconcile: str = Header(default=""),
    connection: asyncpg.Connection = Depends(get_connection),
) -> ReconcileReport:
    """Ask the gateway what happened to every payment we are still unsure about.

    The decision for each row is made from Razorpay's own list of attempts against the
    order, never from elapsed time alone:

    **Captured** — money is in. Settled as paid, and access is granted here exactly as it
    would have been by a webhook. This is the case that makes the reconciler worth having.

    **Authorised but not captured** — money is held and not taken. Auto-capture is on, so
    this should not persist; when it does, something is wrong that a timer cannot fix.
    Routed to manual review rather than failed, because telling a student it failed while
    their bank shows a hold is the worst of both answers.

    **Nothing, or only dead attempts** — failed, but only once the give-up window has
    passed. Before that it stays pending: a student who is still typing an OTP has no
    attempts against their order yet, and failing them mid-payment would be a bug that
    only ever bites the people on slow connections.
    """
    _authorise(x_jeene_reconcile)

    # Billing switched off is a *configuration*, not a fault, and answering it with an
    # error was wrong twice over. It made the cron fail every five minutes from the moment
    # it was created until the day billing was turned on — which teaches whoever set it up
    # to ignore a red run on the one job whose whole purpose is to be trusted when it
    # complains. And it broke the rollback: switching billing off is the lever to pull when
    # something is wrong at eleven at night, and pulling it would have started an alarm
    # storm on top of whatever was already going on.
    #
    # There is genuinely nothing to sweep. Nothing can be sold, so nothing can be pending.
    if not settings.jeene_billing_enabled:
        logger.info("reconcile: billing is switched off; nothing to sweep")
        return ReconcileReport(status="disabled", examined=0, paid=0, failed=0,
                               review=0, left_pending=0)

    # Switched *on* with no credentials behind it is a different thing entirely: a
    # deployment that believes it is selling and cannot confirm a payment. That still
    # fails loudly, because somebody has to find out.
    try:
        gw = gateway()
    except BillingUnavailable as exc:
        logger.error("reconcile: billing is enabled but has no gateway credentials")
        raise HTTPException(status_code=503, detail="Payments are not configured") from exc

    rows = await connection.fetch(
        """
        SELECT order_id, EXTRACT(EPOCH FROM (now() - created_at))::bigint AS age_seconds
          FROM payments
         WHERE status = 'pending'
           AND created_at < now() - make_interval(secs => $1)
         ORDER BY created_at
         LIMIT $2
        """,
        _GRACE_SECONDS, _BATCH,
    )

    report = ReconcileReport(examined=0, paid=0, failed=0, review=0, left_pending=0)
    started = time.monotonic()

    for row in rows:
        if time.monotonic() - started > _BUDGET_SECONDS:
            logger.info("reconcile ran out of budget with %d rows left", len(rows) - report.examined)
            break

        order_id = row["order_id"]
        report.examined += 1

        try:
            attempts = await gw.fetch_order_payments(order_id)
        except Exception:  # noqa: BLE001 — one unreachable order must not end the sweep
            logger.warning("reconcile could not reach the gateway for %s", order_id)
            report.left_pending += 1
            continue

        captured = next((p for p in attempts if p.captured), None)
        held = next((p for p in attempts if p.status == "authorized"), None)
        expired = row["age_seconds"] >= _GIVE_UP_SECONDS

        try:
            if captured is not None:
                outcome = await settle.settle(
                    connection, order_id, to=settle.PAID,
                    payment_id=captured.payment_id, payload=captured.raw,
                    paid_paise=captured.amount_paise,
                )
                logger.info("reconcile settled %s as %s (granted=%s)",
                            order_id, outcome.status, outcome.granted)
                # Not necessarily paid: a short payment is sent to a person instead, and
                # the count has to say what happened rather than what was asked for.
                if outcome.status == settle.PAID:
                    report.paid += 1
                else:
                    report.review += 1

            elif held is not None and expired:
                await settle.settle(
                    connection, order_id, to=settle.REVIEW,
                    payment_id=held.payment_id,
                    failure_reason="authorised but never captured",
                    payload=held.raw,
                )
                logger.error("reconcile found %s authorised and uncaptured", order_id)
                report.review += 1

            elif expired:
                dead = next((p for p in attempts if p.dead), None)
                await settle.settle(
                    connection, order_id, to=settle.FAILED,
                    payment_id=dead.payment_id if dead else None,
                    failure_reason=(dead.failure_reason if dead and dead.failure_reason
                                    else "the payment was not completed"),
                    payload=dead.raw if dead else None,
                )
                report.failed += 1

            else:
                report.left_pending += 1

        except settle.UnknownOrder:
            # Impossible — the id came out of this table a moment ago — but a sweep that
            # dies on one row is a sweep that stops settling the rest.
            logger.error("reconcile lost the payment row for %s", order_id)

    return report

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
