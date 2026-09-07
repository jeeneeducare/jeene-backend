"""The one place a payment's outcome is decided.

Four things confirm a purchase and they are deliberately independent, because each fails
in a way the others do not: the app can be killed, a webhook can be dropped, the
reconciler can be a few minutes behind, a network can vanish mid-capture. All four call
this module and nothing else writes `payments.status`.

That is only safe because settling is idempotent, and it is idempotent for two reasons
that are worth separating:

**The row is locked.** `SELECT … FOR UPDATE` inside a transaction, so two paths arriving
together are serialised by Postgres rather than by hope.

**Terminal states are sticky.** A settled payment absorbs every later confirmation as a
no-op with a timestamp bump. A duplicate webhook is the normal case, not an error, and
treating it as one would mean alerting on healthy traffic.

Two things send a payment to the fourth state instead of to `paid`, and both are cases
where the right answer is a person rather than a rule:

**A late capture.** Money arrives *after* we have already given up and failed the row — a
real race, rare and survivable. Granting quietly would hide a student who has likely been
told it failed and paid again; failing it would keep money we took.

**A short payment.** Less arrived than was charged. Partial payments are not enabled, so
this should be impossible; if it ever happens, granting full access for part of the price
is the one outcome that cannot be undone by looking at it later.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import asyncpg

from app.billing import entitlements

logger = logging.getLogger(__name__)

PENDING = "pending"
PAID = "paid"
FAILED = "failed"
REVIEW = "needs_manual_review"

TERMINAL = (PAID, FAILED, REVIEW)


class UnknownOrder(Exception):
    """No local record of this order. Never raised for an order we created."""


@dataclass(frozen=True)
class Settlement:
    """What this call did, for the caller's log line and for the app's next screen."""

    status: str
    #: Whether this call was the one that moved the row. False means somebody got here
    #: first, which on three of the four paths is entirely expected.
    changed: bool
    #: True only when this call inserted the grant. Exactly one caller ever sees this.
    granted: bool = False


async def settle(
    connection: asyncpg.Connection,
    order_id: str,
    *,
    to: str,
    payment_id: str | None = None,
    failure_reason: str = "",
    payload: dict[str, Any] | None = None,
    paid_paise: int | None = None,
) -> Settlement:
    """Move a payment to a terminal state, exactly once.

    Opens its own transaction, so every caller gets the row lock whether or not it
    remembered to ask for one. The grant and the expiry cache are written inside it: a
    student is never Pro in one table and not in another, not even for a moment.

    `paid_paise` is what the gateway says actually arrived, when the caller knows it. It
    is checked against the amount on the row rather than against the product, because the
    row is what was charged — and it is checked here rather than at each call site so that
    all four confirmation paths get the same answer.
    """
    if to not in (PAID, FAILED, REVIEW):
        raise ValueError(f"not a settlement: {to}")

    async with connection.transaction():
        row = await connection.fetchrow(
            """
            SELECT order_id, firebase_uid, tenant_id, status, duration_days,
                   amount_paise, razorpay_payment_id
              FROM payments WHERE order_id = $1
             FOR UPDATE
            """,
            order_id,
        )
        if row is None:
            raise UnknownOrder(order_id)

        if row["status"] in TERMINAL:
            return await _absorb(connection, row, to, payment_id, payload)

        if to == PAID and paid_paise is not None and paid_paise < row["amount_paise"]:
            # Should be unreachable: partial payments are not enabled on the orders we
            # create. Unreachable code that grants access is worth writing anyway.
            logger.error("short payment on %s: %s of %s paise",
                         order_id, paid_paise, row["amount_paise"])
            to = REVIEW
            failure_reason = (
                f"only {paid_paise} of {row['amount_paise']} paise arrived"
            )

        await _write(connection, order_id, to, payment_id, failure_reason, payload)

        granted = False
        if to == PAID:
            granted = await entitlements.grant(
                connection,
                uid=row["firebase_uid"],
                tenant=row["tenant_id"],
                days=row["duration_days"],
                provider="razorpay",
                order_id=order_id,
            )
        return Settlement(status=to, changed=True, granted=granted)


async def _absorb(
    connection: asyncpg.Connection,
    row: asyncpg.Record,
    to: str,
    payment_id: str | None,
    payload: dict[str, Any] | None,
) -> Settlement:
    """A confirmation for a payment that is already settled.

    Three of the four paths race by design, so arriving second is the common case and
    costs nothing but a timestamp. The exception is money landing on a row we had already
    failed, which no amount of idempotency makes routine.
    """
    current = row["status"]

    if current == FAILED and to == PAID:
        logger.error(
            "late capture on a failed payment: %s — routed to manual review",
            row["order_id"],
        )
        await _write(connection, row["order_id"], REVIEW, payment_id,
                     "captured after the payment was given up on", payload)
        return Settlement(status=REVIEW, changed=True)

    if current == PAID and to == FAILED:
        # Money is in. A client or a stale webhook saying otherwise is wrong, and acting
        # on it would revoke access somebody paid for.
        logger.warning("ignored a failure report for a paid payment: %s", row["order_id"])

    await connection.execute(
        "UPDATE payments SET updated_at = now() WHERE order_id = $1", row["order_id"]
    )
    return Settlement(status=current, changed=False)


async def _write(
    connection: asyncpg.Connection,
    order_id: str,
    to: str,
    payment_id: str | None,
    failure_reason: str,
    payload: dict[str, Any] | None,
) -> None:
    """The single UPDATE. Kept here so no caller writes `status` by hand.

    `payload` is handed to asyncpg as a dict, not as JSON text. Every pooled connection
    carries a jsonb codec that serialises it, so encoding here too would store the
    gateway's answer as a JSON *string* containing JSON — legal, unqueryable, and exactly
    the wrong thing to find in front of you the day somebody disputes a charge.
    """
    await connection.execute(
        """
        UPDATE payments
           SET status = $2,
               razorpay_payment_id = COALESCE($3, razorpay_payment_id),
               failure_reason = CASE WHEN $4 <> '' THEN $4 ELSE failure_reason END,
               gateway_payload = CASE WHEN $5::jsonb IS NOT NULL
                                      THEN $5::jsonb ELSE gateway_payload END,
               settled_at = COALESCE(settled_at, now()),
               updated_at = now()
         WHERE order_id = $1
        """,
        order_id, to, payment_id, failure_reason, payload,
    )


async def record_failed_attempt(
    connection: asyncpg.Connection,
    order_id: str,
    *,
    payment_id: str | None,
    reason: str,
    payload: dict[str, Any] | None = None,
) -> bool:
    """Note that one attempt failed, without ending the payment. Returns whether it stuck.

    One order can be paid at more than one attempt. A mistyped CVV, a bank that declines
    once and approves on the retry, a UPI app that times out — Razorpay sends
    `payment.failed` for each of those and then `payment.captured` when the student
    succeeds, against the **same order**.

    So a failed attempt must not settle anything. Treating it as terminal would take a
    perfectly ordinary purchase, mark it failed, and then route the capture that follows
    seconds later into manual review — for a student who did nothing wrong and is looking
    at a success screen.

    What is worth keeping is the reason, for the support conversation that sometimes
    follows. Written only to a row that is still pending, so this can never disturb a
    payment that has already been decided.
    """
    updated = await connection.execute(
        """
        UPDATE payments
           SET failure_reason = CASE WHEN $3 <> '' THEN $3 ELSE failure_reason END,
               razorpay_payment_id = COALESCE(razorpay_payment_id, $2),
               gateway_payload = CASE WHEN $4::jsonb IS NOT NULL
                                      THEN $4::jsonb ELSE gateway_payload END,
               updated_at = now()
         WHERE order_id = $1 AND status = 'pending'
        """,
        order_id, payment_id, reason, payload,
    )
    return updated.endswith(" 1")
