"""What Pro costs, buying it, and whether this student has it.

Reads first — the catalogue and "am I Pro" — then the write path in the order a purchase
travels it: quote, order, and the two confirmations an app can send.

Nothing in this file decides a payment's outcome by itself. Every route that ends a
purchase hands off to [app.billing.settle], which is the only thing that writes
`payments.status`, and which the webhook and the reconciler call on their own paths. That
is what makes it safe for four things to confirm the same payment at once.

Three decisions worth knowing before reading:

**Products are public.** The paywall renders before sign-in, because a student deciding
whether to make an account should be able to see what it would cost. The reply carries
prices and durations and nothing about anybody.

**Prices are integers, all the way out.** `amount_paise` reaches the app as paise and is
formatted at the very last moment, in the UI. No float touches a price on either side of
the wire — the arithmetic that produces 119999.49999999999 on one runtime and 119999.5 on
another has no place anywhere near a charge.

**The client is never believed about money.** It reports what it saw — a success, a
cancellation — and each report is checked against Razorpay before anything is written. A
patched app can send whatever it likes to these routes and change nothing.
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.auth import current_tenant, optional_user, require_user
from app.billing import BillingUnavailable, entitlements, gateway, quotes, settle
from app.config import settings
from app.db import get_connection
from app.schemas import (
    EntitlementView,
    FailureRequest,
    OrderRequest,
    OrderResponse,
    Product,
    QuoteRequest,
    QuoteResponse,
    SettlementResponse,
    VerifyRequest,
)

logger = logging.getLogger(__name__)

#: Creating an order costs a call to Razorpay and writes a row. Neither is expensive, but
#: an unbounded loop of them is a way to fill a table and annoy a gateway, and no honest
#: client needs more than a handful. Same shape as the handoff-code limiter in tests.py:
#: in-process and approximate, which is the right size of defence for something the
#: authenticated user is already identified for.
_ORDERS_PER_HOUR = 10
_recent_orders: dict[str, list[float]] = defaultdict(list)


def _rate_limit_orders(uid: str) -> None:
    now = time.time()
    hits = [t for t in _recent_orders[uid] if now - t < 3600]
    if len(hits) >= _ORDERS_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="That is a lot of attempts in an hour. Try again shortly.",
        )
    hits.append(now)
    _recent_orders[uid] = hits


router = APIRouter(prefix="/billing", tags=["billing"])


async def _owned(connection: asyncpg.Connection, order_id: str, uid: str) -> asyncpg.Record:
    """The caller's payment, or 404.

    404 rather than 403 for an order belonging to somebody else. Distinguishing "not
    yours" from "does not exist" tells an enumerator which order ids are real.
    """
    row = await connection.fetchrow(
        "SELECT order_id, firebase_uid, tenant_id, status FROM payments WHERE order_id = $1",
        order_id,
    )
    if row is None or row["firebase_uid"] != uid:
        raise HTTPException(status_code=404, detail="No such payment")
    return row


#: A failure reason is the only free text a client can put in this table, and it is read
#: back by a person in an admin panel. Kept to printable characters and one line, so what
#: lands in the database is a sentence rather than whatever fitted in the field.
_REASON_LIMIT = 200


def _clean_reason(reason: str) -> str:
    """The client's account of what went wrong, made safe to store and to read."""
    flattened = " ".join((reason or "").split())
    printable = "".join(c for c in flattened if c.isprintable())
    return printable[:_REASON_LIMIT]


async def _entitlement_view(
    connection: asyncpg.Connection, uid: str, tenant: str
) -> EntitlementView:
    held = await entitlements.entitlement_of(connection, uid, tenant)
    return EntitlementView(
        tier=held.tier, active=held.active,
        expires_at=held.expires_at, days_remaining=held.days_remaining,
    )


@router.get("/products", response_model=list[Product])
async def list_products(
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[Product]:
    """What this tenant sells, cheapest first by the order an admin chose.

    Open to anonymous callers, like the catalogue. A price is not a secret, and a paywall
    that cannot be read until you have signed up is a paywall nobody signs up for.

    Retired products are excluded here and nowhere else: a payment made against one still
    resolves, because `payments` copies the price and duration at creation rather than
    joining back to a row that may since have been switched off.
    """
    rows = await connection.fetch(
        """
        SELECT product_id, title, tier, amount_paise, currency, duration_days,
               badge, sort_order
          FROM products
         WHERE tenant_id = $1 AND active
         ORDER BY sort_order, amount_paise
        """,
        tenant,
    )
    return [Product(**dict(row)) for row in rows]


@router.get("/me", response_model=EntitlementView)
async def my_entitlement(
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> EntitlementView:
    """Whether this student has Pro, and until when.

    The app polls this after a purchase, so it is deliberately cheap: one indexed read of
    the cache that every grant writes.

    `active` is decided here rather than by comparing `expires_at` on the device. A phone
    with a wrong clock — or a deliberately wrong one — must not be able to unlock
    anything, and the gates on the write endpoints ask this same code regardless.
    """
    return await _entitlement_view(connection, user["uid"], tenant)


@router.post("/quote", response_model=QuoteResponse)
async def quote(
    body: QuoteRequest,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> QuoteResponse:
    """What this product costs this student, signed so it cannot be edited.

    The price is read from the database here and read from the database *again* at order
    time. This token is not the authority on what to charge — it is a record of what was
    promised, so the order endpoint can notice if the two have diverged and refuse rather
    than silently charging the newer figure to somebody who agreed to the older one.
    """
    row = await connection.fetchrow(
        """
        SELECT product_id, title, amount_paise, currency, duration_days
          FROM products
         WHERE product_id = $1 AND tenant_id = $2 AND active
        """,
        body.product_id, tenant,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="That is not something we sell")

    token = quotes.sign(
        uid=user["uid"],
        product_id=row["product_id"],
        amount_paise=row["amount_paise"],
        currency=row["currency"],
        duration_days=row["duration_days"],
    )
    return QuoteResponse(
        quote=token,
        product_id=row["product_id"],
        title=row["title"],
        amount_paise=row["amount_paise"],
        currency=row["currency"],
        duration_days=row["duration_days"],
        expires_in_seconds=quotes.DEFAULT_TTL_SECONDS,
    )


@router.post("/orders", response_model=OrderResponse)
async def create_order(
    body: OrderRequest,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> OrderResponse:
    """Turn a quote into an order the checkout SDK can open.

    Three things happen in a fixed order, and the order is the point.

    **The price is recomputed.** The quote says what was promised; the database says what
    the product costs now. If they disagree the purchase is refused, because charging
    either figure would be wrong — the new one was never agreed to, and the old one is no
    longer the price.

    **Razorpay is called before anything is written locally.** If this process dies
    between the two, what is left is an unpaid order at Razorpay, which Razorpay expires
    on its own. Writing the row first would leave a payment pending against an order that
    exists nowhere, and the reconciler would have to invent a way to tell that apart from
    a genuine one.

    **The amount and duration are copied onto the row.** They are never read back from
    `products` again. A price change tomorrow must not rewrite what somebody paid today,
    and a retired product must still produce a legible receipt.
    """
    _rate_limit_orders(user["uid"])

    try:
        gw = gateway()
    except BillingUnavailable as exc:
        logger.error("order refused: %s", exc)
        raise HTTPException(
            status_code=503, detail="Payments are not available right now"
        ) from exc

    try:
        promised = quotes.verify(body.quote, uid=user["uid"])
    except quotes.QuoteInvalid as exc:
        # One message for every way a quote can be wrong. Which one it was is not the
        # caller's to learn.
        raise HTTPException(
            status_code=400, detail="That price is no longer valid. Please try again."
        ) from exc

    row = await connection.fetchrow(
        """
        SELECT product_id, title, amount_paise, currency, duration_days
          FROM products
         WHERE product_id = $1 AND tenant_id = $2 AND active
        """,
        promised.product_id, tenant,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="That is not something we sell")

    if (row["amount_paise"], row["currency"], row["duration_days"]) != (
        promised.amount_paise, promised.currency, promised.duration_days
    ):
        logger.warning(
            "quote/product mismatch for %s: promised %s, now %s",
            promised.product_id, promised.amount_paise, row["amount_paise"],
        )
        raise HTTPException(
            status_code=409, detail="The price changed. Please take another look."
        )

    try:
        order = await gw.create_order(
            amount_paise=row["amount_paise"],
            currency=row["currency"],
            # Razorpay caps a receipt at 40 characters.
            receipt=f"jeene_{user['uid'][:28]}",
            notes={"firebase_uid": user["uid"], "product_id": row["product_id"],
                   "tenant_id": tenant},
        )
    except Exception as exc:  # noqa: BLE001 — every gateway failure is one answer here
        logger.exception("could not create a Razorpay order")
        raise HTTPException(
            status_code=502, detail="We could not reach the payment provider"
        ) from exc

    await connection.execute(
        """
        INSERT INTO payments (order_id, firebase_uid, tenant_id, product_id,
                              amount_paise, currency, duration_days, status)
        VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
        """,
        order.order_id, user["uid"], tenant, row["product_id"],
        row["amount_paise"], row["currency"], row["duration_days"],
    )

    return OrderResponse(
        order_id=order.order_id,
        key_id=settings.razorpay_key_id or "",
        amount_paise=row["amount_paise"],
        currency=row["currency"],
        product_id=row["product_id"],
        title=row["title"],
    )


@router.post("/verify", response_model=SettlementResponse)
async def verify_payment(
    body: VerifyRequest,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> SettlementResponse:
    """The app saying checkout succeeded. Checked twice before it is believed.

    **The signature** proves the callback really came from Razorpay and was not typed by
    a patched client. It does not prove money moved: an authorised-but-uncaptured payment
    signs identically.

    **The fetch** is what proves it. A server-to-server read of the payment is the only
    thing that can say `captured`, and nothing is granted without it.

    A payment that turns out not to be captured is *not* failed here. It may still be in
    flight, and failing it would release a student who is about to pay. The reconciler
    decides that later, when a few minutes have made the answer unambiguous.
    """
    await _owned(connection, body.order_id, user["uid"])

    try:
        gw = gateway()
    except BillingUnavailable as exc:
        raise HTTPException(status_code=503, detail="Payments are not available") from exc

    if not gw.verify_payment_signature(
        order_id=body.order_id, payment_id=body.payment_id, signature=body.signature
    ):
        logger.warning("bad payment signature on %s", body.order_id)
        raise HTTPException(status_code=400, detail="We could not verify that payment")

    try:
        payment = await gw.fetch_payment(body.payment_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not fetch payment %s", body.payment_id)
        raise HTTPException(
            status_code=502, detail="We could not confirm that payment just now"
        ) from exc

    if payment.order_id and payment.order_id != body.order_id:
        # The signature covers this pairing, so reaching here means something stranger
        # than a tampered request. Refuse rather than reason about it.
        logger.error("payment %s is for order %s, not %s",
                     body.payment_id, payment.order_id, body.order_id)
        raise HTTPException(status_code=400, detail="We could not verify that payment")

    if not payment.captured:
        # Deliberately not a settlement. See the docstring.
        current = await _owned(connection, body.order_id, user["uid"])
        return SettlementResponse(order_id=body.order_id, status=current["status"])

    outcome = await settle.settle(
        connection, body.order_id, to=settle.PAID,
        payment_id=payment.payment_id, payload=payment.raw,
        paid_paise=payment.amount_paise,
    )
    return SettlementResponse(
        order_id=body.order_id,
        status=outcome.status,
        entitlement=await _entitlement_view(connection, user["uid"], tenant),
    )


@router.post("/failed", response_model=SettlementResponse)
async def report_failure(
    body: FailureRequest,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> SettlementResponse:
    """The app reporting checkout did not complete. Confirmed before it is acted on.

    This exists to release a pending row in a second rather than in ten minutes, so a
    student who cancels can try again immediately instead of being told a payment is
    still going through.

    A client claim never fails a payment on its own. The gateway is asked first, and two
    answers override the report:

    **Something was captured.** The report is ignored and the payment is settled as paid —
    otherwise a patched client could mark its own successful payment as failed and keep
    the access.

    **Something is still in flight.** A UPI intent handed off to another app looks exactly
    like an abandoned checkout from here, right up until it does not. Leaving the row
    pending costs the student nothing; failing it while their bank is still deciding would
    send the capture that follows into manual review.
    """
    await _owned(connection, body.order_id, user["uid"])

    try:
        gw = gateway()
        attempts = await gw.fetch_order_payments(body.order_id)
    except BillingUnavailable as exc:
        raise HTTPException(status_code=503, detail="Payments are not available") from exc
    except Exception as exc:  # noqa: BLE001
        # Cannot confirm, so nothing is written. The reconciler will settle it.
        logger.warning("could not check order %s before failing it", body.order_id)
        raise HTTPException(
            status_code=502, detail="We could not confirm that just now"
        ) from exc

    captured = next((p for p in attempts if p.captured), None)
    if captured is None and any(p.in_flight for p in attempts):
        logger.info("failure report held: %s still has an attempt in flight", body.order_id)
        current = await _owned(connection, body.order_id, user["uid"])
        return SettlementResponse(order_id=body.order_id, status=current["status"])

    if captured is not None:
        logger.info("failure report ignored: %s was captured", body.order_id)
        outcome = await settle.settle(
            connection, body.order_id, to=settle.PAID,
            payment_id=captured.payment_id, payload=captured.raw,
            paid_paise=captured.amount_paise,
        )
        return SettlementResponse(
            order_id=body.order_id, status=outcome.status,
            entitlement=await _entitlement_view(connection, user["uid"], tenant),
        )

    outcome = await settle.settle(
        connection, body.order_id, to=settle.FAILED,
        failure_reason=_clean_reason(body.reason) or "the payment was not completed",
    )
    return SettlementResponse(order_id=body.order_id, status=outcome.status)


@router.post("/webhook", include_in_schema=False)
async def webhook(
    request: Request,
    x_razorpay_signature: str = Header(default=""),
    connection: asyncpg.Connection = Depends(get_connection),
) -> dict[str, str]:
    """Razorpay's own account of what happened.

    No user authentication, because Razorpay has no user token — the HMAC over the raw
    body is the authentication, and it is the only thing standing between this route and
    anybody who can reach the internet.

    The signature is checked against the **unparsed bytes**. Re-serialising the JSON
    changes whitespace and key order, and the signature covers what was sent rather than
    what it meant.

    Always answers 200 once the signature is good, even for an event that changes
    nothing. A non-200 tells Razorpay to redeliver, and asking to be told again about
    something already handled is how a retry storm starts.

    Only a capture settles anything here. `payment.failed` describes **one attempt**, not
    the order: a student who mistypes a CVV and then pays produces exactly that pair of
    events against the same order id, and treating the first as final would put an
    ordinary successful purchase into manual review. Ending an unpaid payment is left to
    the two paths that can tell an abandoned checkout from a retry — the app, which knows
    the sheet was closed, and the reconciler, which waits half an hour and asks.
    """
    raw = await request.body()

    try:
        gw = gateway()
    except BillingUnavailable:
        # Nothing can be verified, so nothing may be believed.
        logger.error("webhook arrived while billing is unconfigured")
        raise HTTPException(status_code=503, detail="unavailable") from None

    if not gw.verify_webhook_signature(body=raw, signature=x_razorpay_signature):
        logger.warning("webhook with a bad signature, %d bytes", len(raw))
        raise HTTPException(status_code=400, detail="bad signature")

    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(status_code=400, detail="bad body") from None

    name = event.get("event", "")
    payload = event.get("payload", {})

    # `order.paid` carries both entities; the payment one is preferred because it is the
    # only place the payment id lives, and that id is what a refund is issued against.
    payment = payload.get("payment", {}).get("entity", {})
    entity = payment or payload.get("order", {}).get("entity", {})
    order_id = entity.get("order_id") or entity.get("id", "")
    if not order_id:
        logger.info("webhook %s carried no order id", name)
        return {"status": "ignored"}

    if name == "payment.failed":
        noted = await settle.record_failed_attempt(
            connection, order_id,
            payment_id=payment.get("id"),
            reason=entity.get("error_description") or "",
            payload=entity,
        )
        logger.info("attempt failed on %s (%s); payment left open",
                    order_id, entity.get("error_description") or "no reason given")
        return {"status": "noted" if noted else "ignored"}

    if name not in ("payment.captured", "order.paid"):
        return {"status": "ignored"}

    try:
        amount = payment.get("amount")
        outcome = await settle.settle(
            connection, order_id, to=settle.PAID,
            payment_id=payment.get("id"),
            payload=entity,
            paid_paise=int(amount) if amount is not None else None,
        )
    except settle.UnknownOrder:
        # An order this deployment did not create — a webhook aimed at the wrong
        # environment, most likely. Acknowledged so it is not redelivered for ever.
        logger.warning("webhook for an unknown order: %s", order_id)
        return {"status": "unknown"}

    logger.info("webhook %s settled %s as %s (changed=%s)",
                name, order_id, outcome.status, outcome.changed)
    return {"status": outcome.status}
