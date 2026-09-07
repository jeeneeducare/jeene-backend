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

import asyncpg
from fastapi import APIRouter, Depends

from app.auth import current_tenant, optional_user, require_user
from app.billing import entitlements
from app.db import get_connection
from app.schemas import EntitlementView, Product

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
