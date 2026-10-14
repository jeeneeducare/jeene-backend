"""What a store notification means for the ledger.

Shared by the two notification routes and by the reconciler, which reads the same
notifications back when the routes missed them. One function per store, each returning
a word for the log line, never raising for anything that is not worth retrying: a
notification about a purchase we cannot attribute is acknowledged and dropped, because
redelivering it would not make it attributable.

What each store says, and what is done about it:

**Google** sends a `DeveloperNotification` through Pub/Sub. It says only *that* something
changed — "a one-time product was purchased", "a purchase was voided" — so a purchase is
always read back from Google's API before anything is granted. A voided purchase needs no
read: Google voiding it is the fact.

**Apple** sends the transaction itself, signed. `ONE_TIME_CHARGE` grants it, `REFUND` and
`REVOKE` take it back, `REFUND_REVERSED` gives it back. Everything else is about
subscriptions, which Jeene does not sell.
"""

from __future__ import annotations

import logging
from typing import Any

import asyncpg

from app.billing import purchases
from app.billing.stores import APPLE, GOOGLE, AppleNotification, GooglePlay

logger = logging.getLogger(__name__)

#: `voidedPurchaseNotification.productType` for a subscription. Jeene sells none, and a
#: subscription's token would not be in `store_purchases` anyway.
_GOOGLE_SUBSCRIPTION = 1


async def handle_google(
    connection: asyncpg.Connection, client: GooglePlay, notification: dict[str, Any]
) -> str:
    """One `DeveloperNotification`, already authenticated and decoded."""
    if notification.get("packageName") != client.package_name:
        logger.warning("Google notification for another package: %s",
                       notification.get("packageName"))
        return "ignored: another package"
    if "testNotification" in notification:
        logger.info("Google test notification received")
        return "test"

    one_time = notification.get("oneTimeProductNotification")
    if isinstance(one_time, dict) and one_time.get("purchaseToken"):
        # Raises StoreUnreachable if Google cannot be asked, which the route turns into a
        # 503 so Pub/Sub delivers this again later.
        purchase = await client.get_purchase(one_time["purchaseToken"])
        try:
            owner = await purchases.owner_of(connection, purchase, handed_over_by=None)
        except purchases.UnknownAccount:
            logger.warning("Google purchase names an account we do not know; dropped")
            return "ignored: unknown account"
        if owner is None:
            # Made without our id. If it is ours, the app hands it over itself.
            return "ignored: no account on the purchase"
        try:
            outcome = await purchases.record(connection, purchase, owner)
        except purchases.NotForSale:
            logger.warning("Google purchase of %r, which no pass is", purchase.store_product_id)
            return "ignored: not a pass"
        await purchases.consume_if_due(connection, client, purchase, outcome)
        return outcome.status

    voided = notification.get("voidedPurchaseNotification")
    if isinstance(voided, dict) and voided.get("purchaseToken"):
        if voided.get("productType") == _GOOGLE_SUBSCRIPTION:
            return "ignored: subscription"
        changed = await purchases.void(connection, GOOGLE, voided["purchaseToken"])
        return "refunded" if changed else "ignored: nothing to take back"

    return "ignored"


async def handle_apple(connection: asyncpg.Connection, note: AppleNotification) -> str:
    """One App Store Server Notification, already verified."""
    if note.kind == "TEST":
        logger.info("Apple test notification received")
        return "test"
    purchase = note.purchase
    if purchase is None:
        return f"ignored: {note.kind}"

    if note.kind == "ONE_TIME_CHARGE":
        try:
            owner = await purchases.owner_of(connection, purchase, handed_over_by=None)
        except purchases.UnknownAccount:
            logger.warning("Apple purchase names an account we do not know; dropped")
            return "ignored: unknown account"
        if owner is None:
            return "ignored: no account on the purchase"
        try:
            outcome = await purchases.record(connection, purchase, owner)
        except purchases.NotForSale:
            logger.warning("Apple purchase of %r, which no pass is", purchase.store_product_id)
            return "ignored: not a pass"
        return outcome.status

    if note.kind in ("REFUND", "REVOKE"):
        changed = await purchases.void(connection, APPLE, purchase.ref)
        return "refunded" if changed else "ignored: nothing to take back"

    if note.kind == "REFUND_REVERSED":
        changed = await purchases.restore(connection, APPLE, purchase.ref)
        return "restored" if changed else "ignored: nothing to give back"

    return f"ignored: {note.kind}"
