"""The one place a store purchase's outcome is decided.

Three things tell this backend about a purchase, and each fails in a way the others do
not: the app can die between the store's sheet and its next request; a store
notification can be dropped, or arrive during a deploy; the reconciler only runs every
few minutes. All three call this module and nothing else writes `store_purchases` or
grants a store purchase.

That is safe for the same two reasons settling a Razorpay payment was:

**The rows are locked, student first.** The student's row, then the purchase's, inside
one transaction — the order [app.billing.entitlements.grant] takes them in, because the
other order deadlocks against it.

**Every step is idempotent.** One purchase row per `(store, store_ref)`, one grant per
purchase, one reversal per grant. A notification arriving twice, or a notification and
the app arriving together, end in one grant decided by the database.

Who a purchase belongs to is decided here too, and in a fixed order. A purchase already
recorded belongs to whoever it was recorded for — the first path to arrive wins, and a
second account cannot claim it later. A new purchase belongs to the account whose private
id the store carried back, whoever happens to be signed in on that phone now. Only a
purchase that carries no id at all falls back to the student who handed it over.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime

import asyncpg

from app.billing import entitlements
from app.billing.stores import (
    APPLE,
    CANCELLED,
    GOOGLE,
    PENDING,
    PURCHASED,
    REVOKED,
    GooglePlay,
    ProofRejected,
    StorePurchase,
    StoreUnreachable,
)

logger = logging.getLogger(__name__)

# Where a purchase ends up, as the app is told it.
GRANTED = "granted"
ALREADY_GRANTED = "already_granted"
STATUS_PENDING = "pending"
STATUS_CANCELLED = "cancelled"
REFUNDED = "refunded"
#: Recorded and granted — to the account the purchase names, which is not the one that
#: handed it over.
OTHER_ACCOUNT = "other_account"

_PRODUCT_COLUMN = {GOOGLE: "google_product_id", APPLE: "apple_product_id"}


class UnknownAccount(Exception):
    """The purchase names an account this backend has never issued, or no one to credit."""


class NotForSale(Exception):
    """No pass is sold under this store product id, for this student's tenant."""


@dataclass(frozen=True)
class Owner:
    uid: str
    tenant: str


@dataclass(frozen=True)
class Outcome:
    """What one call did, for the response and for the log line."""

    status: str
    owner: Owner
    #: True only for the call that inserted the grant. Exactly one caller ever sees it.
    granted: bool = False


async def owner_of(
    connection: asyncpg.Connection, purchase: StorePurchase, *, handed_over_by: str | None
) -> Owner | None:
    """Whose purchase this is. See the module docstring for the order, and why.

    None only for a purchase that names no account and that nobody handed over — a store
    notification about a purchase made without our id. There is no one to credit, so
    nothing is recorded; if it was ours, the app will hand it over itself.
    """
    recorded = await connection.fetchrow(
        "SELECT firebase_uid, tenant_id FROM store_purchases WHERE store = $1 AND store_ref = $2",
        purchase.store, purchase.ref,
    )
    if recorded is not None:
        return Owner(recorded["firebase_uid"], recorded["tenant_id"])

    if purchase.account_token:
        try:
            token = uuid.UUID(purchase.account_token)
        except ValueError:
            raise UnknownAccount("the purchase carries an id that is not one of ours") from None
        named = await connection.fetchrow(
            "SELECT firebase_uid, tenant_id FROM users WHERE store_account_token = $1", token
        )
        if named is None:
            raise UnknownAccount("the purchase names an account we never issued")
        return Owner(named["firebase_uid"], named["tenant_id"])

    if handed_over_by is None:
        return None
    tenant = await connection.fetchval(
        "SELECT tenant_id FROM users WHERE firebase_uid = $1", handed_over_by
    )
    if tenant is None:
        raise UnknownAccount("the student who handed this over has no profile yet")
    return Owner(handed_over_by, tenant)


async def _product(
    connection: asyncpg.Connection, purchase: StorePurchase, owner: Owner
) -> asyncpg.Record:
    """The pass this store product is. Retired passes still resolve.

    Deliberately not filtered on `active`: a pass switched off this morning was still for
    sale when somebody bought it last night, and their purchase has to land.
    """
    column = _PRODUCT_COLUMN[purchase.store]
    # The column name comes from the fixed map above, never from a request; every value
    # is a parameter.
    row = await connection.fetchrow(
        f"SELECT product_id, duration_days FROM products WHERE {column} = $1 AND tenant_id = $2",
        purchase.store_product_id, owner.tenant,
    )
    if row is None:
        raise NotForSale(f"no pass is sold as {purchase.store_product_id!r}")
    return row


def _jsonable(value: object) -> object:
    """The store's answer as plain JSON. The pool's codec encodes it; this only makes sure it can."""
    return json.loads(json.dumps(value, default=str))


def _initial_status(state: str) -> str:
    return {
        PURCHASED: STATUS_PENDING,  # becomes granted in the same transaction
        PENDING: STATUS_PENDING,
        CANCELLED: STATUS_CANCELLED,
        REVOKED: REFUNDED,
    }[state]


async def record(
    connection: asyncpg.Connection, purchase: StorePurchase, owner: Owner
) -> Outcome:
    """Record what the store says about a purchase, and grant it if it is paid for.

    Each state moves a purchase only forwards:

      * **paid** — granted once, however many paths report it;
      * **pending** — recorded, nothing granted, nothing failed;
      * **cancelled** — only a pending purchase can be cancelled;
      * **revoked** — a granted purchase has its grant reversed, once.

    A stale report never moves one backwards. The app re-sending a transaction Apple
    signed before a refund does not un-refund it, and a pending read that loses a race
    with the grant does not un-grant it.
    """
    product = await _product(connection, purchase, owner)

    async with connection.transaction():
        # The student, then the purchase. See the module docstring.
        await connection.fetchval(
            "SELECT firebase_uid FROM users WHERE firebase_uid = $1 FOR UPDATE", owner.uid
        )
        row = await connection.fetchrow(
            """
            INSERT INTO store_purchases
                   (store, store_ref, store_order_id, store_product_id, product_id,
                    firebase_uid, tenant_id, duration_days, environment, status,
                    price_micros, currency, purchased_at, store_payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
            ON CONFLICT (store, store_ref) DO NOTHING
            RETURNING purchase_id, status, duration_days
            """,
            purchase.store, purchase.ref, purchase.order_id, purchase.store_product_id,
            product["product_id"], owner.uid, owner.tenant, product["duration_days"],
            purchase.environment, _initial_status(purchase.state), purchase.price_micros,
            purchase.currency, purchase.purchased_at, _jsonable(purchase.raw),
        )
        if row is None:
            row = await connection.fetchrow(
                """
                SELECT purchase_id, status, duration_days FROM store_purchases
                 WHERE store = $1 AND store_ref = $2 FOR UPDATE
                """,
                purchase.store, purchase.ref,
            )

        status = row["status"]
        granted = False

        if purchase.state == PURCHASED and status in (STATUS_PENDING, STATUS_CANCELLED):
            granted = await entitlements.grant(
                connection, uid=owner.uid, tenant=owner.tenant, days=row["duration_days"],
                provider=purchase.store, purchase_id=row["purchase_id"],
                note=f"{purchase.store_product_id} ({purchase.environment})",
            )
            status = GRANTED
        elif purchase.state == CANCELLED and status == STATUS_PENDING:
            status = STATUS_CANCELLED
        elif purchase.state == REVOKED and status in (GRANTED, STATUS_PENDING):
            await _reverse_current(connection, row["purchase_id"], note="refunded")
            status = REFUNDED

        await connection.execute(
            """
            UPDATE store_purchases
               SET status = $2,
                   store_payload = $3,
                   consumed_at = CASE WHEN $4 THEN coalesce(consumed_at, now())
                                      ELSE consumed_at END,
                   refunded_at = CASE WHEN $2 = 'refunded' THEN coalesce(refunded_at, now())
                                      ELSE refunded_at END,
                   updated_at = now()
             WHERE purchase_id = $1
            """,
            row["purchase_id"], status, _jsonable(purchase.raw),
            purchase.consumed,
        )

    if status == GRANTED and not granted:
        status = ALREADY_GRANTED
    logger.info("store purchase %s/%s for %s: %s", purchase.store, purchase.store_product_id,
                owner.uid, status)
    return Outcome(status=status, owner=owner, granted=granted)


async def _reverse_current(
    connection: asyncpg.Connection, purchase_id: uuid.UUID, *, note: str
) -> bool:
    link = await entitlements.current_link(connection, purchase_id)
    if link is None or link["days"] <= 0:
        return False
    return await entitlements.reverse(connection, link, note=note)


async def void(
    connection: asyncpg.Connection,
    store: str,
    store_ref: str,
    *,
    voided_at: datetime | None = None,
) -> bool:
    """A store took a purchase back: refund, chargeback, revocation. Returns whether anything changed.

    The days come back off the ledger once, however many times the store says so. A
    purchase this backend never recorded is left alone — there is nothing of ours to
    take back, and no account to take it from.
    """
    uid = await connection.fetchval(
        "SELECT firebase_uid FROM store_purchases WHERE store = $1 AND store_ref = $2",
        store, store_ref,
    )
    if uid is None:
        return False

    async with connection.transaction():
        await connection.fetchval(
            "SELECT firebase_uid FROM users WHERE firebase_uid = $1 FOR UPDATE", uid
        )
        row = await connection.fetchrow(
            """
            SELECT purchase_id, status FROM store_purchases
             WHERE store = $1 AND store_ref = $2 FOR UPDATE
            """,
            store, store_ref,
        )
        if row["status"] == REFUNDED:
            return False
        await _reverse_current(connection, row["purchase_id"], note="refunded")
        await connection.execute(
            """
            UPDATE store_purchases
               SET status = 'refunded', refunded_at = coalesce($2, now()), updated_at = now()
             WHERE purchase_id = $1
            """,
            row["purchase_id"], voided_at,
        )
    logger.info("store purchase %s/%s for %s: refunded", store, store_ref[:12], uid)
    return True


async def restore(connection: asyncpg.Connection, store: str, store_ref: str) -> bool:
    """A store reversed its own refund (Apple's `REFUND_REVERSED`). The days come back, once."""
    uid = await connection.fetchval(
        "SELECT firebase_uid FROM store_purchases WHERE store = $1 AND store_ref = $2",
        store, store_ref,
    )
    if uid is None:
        return False

    async with connection.transaction():
        await connection.fetchval(
            "SELECT firebase_uid FROM users WHERE firebase_uid = $1 FOR UPDATE", uid
        )
        row = await connection.fetchrow(
            """
            SELECT purchase_id, status FROM store_purchases
             WHERE store = $1 AND store_ref = $2 FOR UPDATE
            """,
            store, store_ref,
        )
        if row["status"] != REFUNDED:
            return False
        link = await entitlements.current_link(connection, row["purchase_id"])
        if link is not None and link["days"] < 0:
            await entitlements.reverse(connection, link, note="refund reversed")
        await connection.execute(
            """
            UPDATE store_purchases
               SET status = 'granted', refunded_at = NULL, updated_at = now()
             WHERE purchase_id = $1
            """,
            row["purchase_id"],
        )
    return True


async def consume_if_due(
    connection: asyncpg.Connection,
    client: GooglePlay,
    purchase: StorePurchase,
    outcome: Outcome,
) -> None:
    """Consume a granted Google purchase. After the grant, never before.

    Consuming first and then failing to grant would leave a student charged with nothing
    to show for it and Google believing the matter closed. Granting first and then failing
    to consume leaves a purchase the reconciler consumes on its next run — well inside
    the three days Google allows before it refunds an unacknowledged purchase on its own.
    So a failure here is logged and left, never raised.
    """
    if purchase.store != GOOGLE or purchase.consumed:
        return
    if outcome.status not in (GRANTED, ALREADY_GRANTED):
        return
    try:
        await client.consume(purchase.store_product_id, purchase.ref)
    except (StoreUnreachable, ProofRejected):
        logger.warning("could not consume %s yet; the reconciler will", purchase.ref[:12])
        return
    await mark_consumed(connection, purchase.ref)


async def mark_consumed(connection: asyncpg.Connection, purchase_token: str) -> None:
    await connection.execute(
        """
        UPDATE store_purchases SET consumed_at = coalesce(consumed_at, now()), updated_at = now()
         WHERE store = 'google' AND store_ref = $1
        """,
        purchase_token,
    )
