"""What Pro costs, buying it from a store, and whether this student has it.

Pro is sold through Google Play billing on Android and Apple's in-app purchase on
iPhone. The store shows the price, takes the money and hands the app a proof of
purchase; this file is where that proof arrives and where the stores' own notifications
arrive, and nothing in it grants anything until the store itself has confirmed it.

Reads first — the catalogue and "am I Pro" — then the three ways a purchase reaches us:

  * **`POST /billing/purchases`** — the app handing over a purchase it just completed,
    or one it found unfinished at launch;
  * **`POST /billing/google/notifications`** — Google, through Pub/Sub, about purchases
    that complete later (a UPI payment) and purchases taken back;
  * **`POST /billing/apple/notifications`** — Apple, about the same.

All three hand off to [app.billing.purchases], which is the only thing that records a
store purchase or grants one, and which the reconciler calls on its own path. That is
what makes it safe for them to arrive twice, or together.

**Products are public.** The paywall renders before sign-in, because a student deciding
whether to make an account should be able to see what it would cost.

**The client is never believed.** It hands over the store's proof and nothing else; the
product, the account, whether it is paid and whether it was a test all come from the
store's answer. A patched app can send whatever it likes here and grant itself nothing.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from collections import defaultdict

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.auth import current_tenant, require_user
from app.billing import BillingUnavailable, app_store, entitlements, google_play, purchases
from app.billing.notifications import handle_apple, handle_google
from app.billing.stores import GOOGLE, PENDING, ProofRejected, StoreUnreachable
from app.db import get_connection
from app.schemas import (
    EntitlementView,
    Product,
    StorePurchaseRequest,
    StorePurchaseResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])

#: Handing over a purchase costs a call to Google or a signature check against Apple's
#: certificates. Neither is expensive, but an unbounded loop of them spends a quota that
#: real purchases need, and no honest app needs more than a handful an hour — a restore
#: on a phone with every pass ever bought is still a handful. In-process and
#: approximate, the right size of defence for a caller who is already signed in.
_PURCHASES_PER_HOUR = 30
_recent_purchases: dict[str, list[float]] = defaultdict(list)

#: A store notification is a few kilobytes. The cap is generous by two orders of
#: magnitude and exists only so these two routes, which no user signs, cannot be made to
#: hold an arbitrary amount of memory before their signatures are checked.
_MAX_NOTIFICATION_BYTES = 256 * 1024


def _rate_limit_purchases(uid: str) -> None:
    now = time.time()
    hits = [t for t in _recent_purchases[uid] if now - t < 3600]
    if len(hits) >= _PURCHASES_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="That is a lot of attempts in an hour. Try again shortly.",
        )
    hits.append(now)
    _recent_purchases[uid] = hits


async def _account_token(connection: asyncpg.Connection, uid: str) -> str | None:
    """This student's private purchase id, made the first time it is asked for.

    None only for a student with no profile row yet, who cannot buy until the app has
    synced one — the same rule the old checkout enforced, for the same reason: a purchase
    must have somewhere to land.
    """
    token = await connection.fetchval(
        "SELECT store_account_token FROM users WHERE firebase_uid = $1", uid
    )
    if token is None:
        token = await connection.fetchval(
            """
            UPDATE users SET store_account_token = gen_random_uuid()
             WHERE firebase_uid = $1 AND store_account_token IS NULL
            RETURNING store_account_token
            """,
            uid,
        )
        if token is None:
            # Either there is no such student, or a concurrent request set it first.
            token = await connection.fetchval(
                "SELECT store_account_token FROM users WHERE firebase_uid = $1", uid
            )
    return str(token) if token else None


async def _entitlement_view(
    connection: asyncpg.Connection, uid: str, tenant: str, *, with_token: bool = False
) -> EntitlementView:
    held = await entitlements.entitlement_of(connection, uid, tenant)
    return EntitlementView(
        tier=held.tier, active=held.active,
        expires_at=held.expires_at, days_remaining=held.days_remaining,
        account_token=await _account_token(connection, uid) if with_token else None,
    )


async def _body(request: Request) -> bytes:
    """The raw body of an unauthenticated route, refused before it is read if it is too big."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _MAX_NOTIFICATION_BYTES:
        raise HTTPException(status_code=413, detail="too large")
    raw = await request.body()
    if len(raw) > _MAX_NOTIFICATION_BYTES:
        raise HTTPException(status_code=413, detail="too large")
    return raw


# --- reads -------------------------------------------------------------------------------

@router.get("/products", response_model=list[Product])
async def list_products(
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[Product]:
    """What this tenant sells, in the order an admin chose.

    Open to anonymous callers. A price is not a secret, and a paywall that cannot be read
    until you have signed up is a paywall nobody signs up for.

    Retired passes are excluded here and nowhere else: a purchase of one still resolves,
    because a store purchase is matched to its pass by store product id, active or not.
    """
    rows = await connection.fetch(
        """
        SELECT product_id, title, tier, amount_paise, currency, duration_days,
               badge, sort_order, google_product_id, apple_product_id
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
    """Whether this student has Pro, until when, and the id their purchases carry.

    The app polls this while a purchase is being confirmed, so it is deliberately cheap:
    one indexed read of the cache every grant writes, and one of the token.

    `active` is decided here rather than by comparing `expires_at` on the device. A phone
    with a wrong clock — or a deliberately wrong one — must not be able to unlock
    anything, and the gates on the write endpoints ask this same code regardless.
    """
    return await _entitlement_view(connection, user["uid"], tenant, with_token=True)


# --- the app handing over a purchase -----------------------------------------------------

@router.post("/purchases", response_model=StorePurchaseResponse)
async def submit_purchase(
    body: StorePurchaseRequest,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> StorePurchaseResponse:
    """A purchase the store just completed, or one the app found unfinished.

    Four steps in a fixed order, and the order is the point:

    **The store is asked.** Google's API for an Android purchase; Apple's certificate
    chain for an iPhone one. Nothing the app said about the purchase is used.

    **The owner is decided** — the account the purchase names, not necessarily the one
    signed in. A pass bought by one student and handed over from a phone where a sibling
    has since signed in still lands on the account that paid.

    **It is recorded and, if paid, granted** — once, however many times it is handed
    over.

    **Google is told** — consumed, after the grant and never before. A failure here is
    the reconciler's to retry, not the student's problem.

    Errors are answered so the app knows whether to keep the purchase and try again: a
    5xx means "try again later, keep it", and only a definite answer ever says `finish`.
    """
    _rate_limit_purchases(user["uid"])

    try:
        if body.store == GOOGLE:
            if not body.purchase_token:
                raise HTTPException(status_code=400, detail="A purchase token is needed")
            client = google_play()
            purchase = await client.get_purchase(body.purchase_token)
        else:
            if not body.signed_transaction:
                raise HTTPException(status_code=400, detail="A signed transaction is needed")
            store = app_store()
            purchase = await store.verify_transaction(body.signed_transaction)
    except BillingUnavailable as exc:
        logger.error("purchase refused: %s", exc)
        raise HTTPException(
            status_code=503, detail="Purchases are not available right now"
        ) from exc
    except ProofRejected as exc:
        logger.warning("purchase proof rejected for %s: %s", user["uid"], exc)
        raise HTTPException(
            status_code=400, detail="We could not confirm that purchase with the store."
        ) from exc
    except StoreUnreachable as exc:
        logger.warning("store unreachable confirming a purchase: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="We could not reach the store just now. Your purchase is safe — "
                   "we will confirm it shortly.",
        ) from exc

    if body.product_id != purchase.store_product_id:
        # Not a reason to refuse: the store's answer is the one used. Worth a line in the
        # log, because an app that names the wrong product has a bug somewhere.
        logger.warning("app named %r for a purchase of %r", body.product_id,
                       purchase.store_product_id)

    try:
        owner = await purchases.owner_of(connection, purchase, handed_over_by=user["uid"])
        if owner is None:  # pragma: no cover — handed_over_by is always set here
            raise purchases.UnknownAccount("nobody to credit")
        outcome = await purchases.record(connection, purchase, owner)
    except purchases.UnknownAccount as exc:
        logger.warning("purchase from %s could not be attributed: %s", user["uid"], exc)
        raise HTTPException(
            status_code=409, detail="Please sign in again before buying."
        ) from exc
    except purchases.NotForSale as exc:
        logger.error("a store purchase of something we do not sell: %s", exc)
        raise HTTPException(status_code=400, detail="That is not something we sell") from exc

    if purchase.store == GOOGLE:
        await purchases.consume_if_due(connection, client, purchase, outcome)

    status = outcome.status
    if outcome.owner.uid != user["uid"]:
        status = purchases.OTHER_ACCOUNT
        logger.info("purchase handed over by %s belongs to %s", user["uid"], outcome.owner.uid)

    return StorePurchaseResponse(
        status=status,
        # Only a purchase still pending at the store stays unfinished on the phone.
        finish=purchase.state != PENDING,
        entitlement=await _entitlement_view(connection, user["uid"], tenant),
    )


# --- the stores' own notifications -------------------------------------------------------

@router.post("/google/notifications", include_in_schema=False)
async def google_notification(
    request: Request,
    authorization: str = Header(default=""),
    connection: asyncpg.Connection = Depends(get_connection),
) -> dict[str, str]:
    """Google Play's real-time developer notifications, pushed by Pub/Sub.

    No user and no shared secret: the OpenID token Pub/Sub signs for our subscription is
    the whole of the authentication, and it is checked before the body is looked at.

    Answers 200 once a message is handled — including one that changes nothing — because
    any other answer tells Pub/Sub to deliver it again. The one exception is Google being
    unreachable while a purchase is read back: that is answered 503 on purpose, so the
    message comes back when the answer can be had.
    """
    raw = await _body(request)
    try:
        client = google_play()
    except BillingUnavailable:
        logger.error("Google notification arrived while Google Play is not configured")
        raise HTTPException(status_code=503, detail="unavailable") from None

    try:
        await client.verify_push(authorization)
    except ProofRejected:
        logger.warning("Google notification with a bad token, %d bytes", len(raw))
        raise HTTPException(status_code=401, detail="unauthenticated") from None

    try:
        envelope = json.loads(raw)
        data = envelope["message"]["data"]
        notification = json.loads(base64.b64decode(data))
    except (ValueError, KeyError, TypeError, binascii.Error):
        # Authenticated, so it came from Pub/Sub — but redelivering something unreadable
        # would only loop. Acknowledged and logged.
        logger.error("Google notification we could not read")
        return {"status": "ignored: unreadable"}
    if not isinstance(notification, dict):
        return {"status": "ignored: unreadable"}

    try:
        result = await handle_google(connection, client, notification)
    except StoreUnreachable:
        raise HTTPException(status_code=503, detail="try again") from None
    except ProofRejected:
        logger.warning("Google notification names a purchase Google will not confirm")
        result = "ignored: not confirmed"
    logger.info("Google notification: %s", result)
    return {"status": result}


@router.post("/apple/notifications", include_in_schema=False)
async def apple_notification(
    request: Request,
    connection: asyncpg.Connection = Depends(get_connection),
) -> dict[str, str]:
    """App Store Server Notifications (version 2).

    Apple signs the payload, and the transaction inside it, with the same certificate
    chain as any transaction; both are checked before anything is read. Answers 200 once
    handled, so Apple stops retrying; a forged or unreadable body is refused.
    """
    raw = await _body(request)
    try:
        store = app_store()
    except BillingUnavailable:
        logger.error("Apple notification arrived while the App Store is not configured")
        raise HTTPException(status_code=503, detail="unavailable") from None

    try:
        signed = json.loads(raw).get("signedPayload")
    except (ValueError, AttributeError):
        signed = None
    if not isinstance(signed, str) or not signed:
        raise HTTPException(status_code=400, detail="bad body")

    try:
        note = await store.verify_notification(signed)
    except ProofRejected:
        logger.warning("Apple notification that did not verify, %d bytes", len(raw))
        raise HTTPException(status_code=400, detail="bad signature") from None
    except StoreUnreachable:
        raise HTTPException(status_code=503, detail="try again") from None

    result = await handle_apple(connection, note)
    logger.info("Apple notification %s (%s): %s", note.kind, note.uuid, result)
    return {"status": result}
