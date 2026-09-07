"""What Pro costs, and whether this student has it.

The read-only half of billing. Nothing here moves money or grants anything — that
arrives with the quote, order and settlement routes. What exists today is the paywall's
data and the answer to "am I Pro", which is enough for the apps to draw the screen and
for the gates to be built against.

Two decisions worth knowing before reading:

**Products are public.** The paywall renders before sign-in, because a student deciding
whether to make an account should be able to see what it would cost. The reply carries
prices and durations and nothing about anybody.

**Prices are integers, all the way out.** `amount_paise` reaches the app as paise and is
formatted at the very last moment, in the UI. No float touches a price on either side of
the wire — the arithmetic that produces 119999.49999999999 on one runtime and 119999.5 on
another has no place anywhere near a charge.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_tenant, optional_user, require_user
from app.billing import BillingUnavailable, entitlements, gateway, quotes
from app.config import settings
from app.db import get_connection
from app.schemas import (
    EntitlementView,
    OrderRequest,
    OrderResponse,
    Product,
    QuoteRequest,
    QuoteResponse,
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
    held = await entitlements.entitlement_of(connection, user["uid"], tenant)
    return EntitlementView(
        tier=held.tier,
        active=held.active,
        expires_at=held.expires_at,
        days_remaining=held.days_remaining,
    )


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
