"""The two app stores, behind ports.

Pro is sold through Google Play billing and Apple's in-app purchase. The store takes the
money and the store is the only thing that can say a purchase is real, so everything this
backend needs from either one fits in two small protocols: read a purchase, check a
notification, and — Google only — consume what has been granted.

Behind a Protocol for the same reason the payment gateway was: the code that decides who
gets what is tested without the network, and a third store would be a class, not a
redesign.

Two rules hold for both stores, and they live here rather than at the call sites:

**A purchase is what the store says, never what the app says.** The app hands over a
token or a signed transaction and nothing else; the product, the account, the state and
whether it was a test all come from the store's answer.

**The two stores are translated into one shape.** [StorePurchase] is what both become,
so the ledger code has one set of states to reason about rather than Google's enum
strings on one path and Apple's revocation dates on another.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

GOOGLE = "google"
APPLE = "apple"

# What a purchase can be, in our words. Each store's own vocabulary is mapped onto these
# in its client and nowhere else.

#: Paid for. The only state that grants anything.
PURCHASED = "purchased"
#: Started and not paid yet — a UPI payment still processing, a parent's Ask to Buy.
#: Nothing is granted, and nothing is failed either: it may still complete.
PENDING = "pending"
#: A pending purchase that will now never complete.
CANCELLED = "cancelled"
#: Paid for once and since taken back by the store: refunded, charged back, revoked.
REVOKED = "revoked"

PRODUCTION = "production"
#: Apple's test environment, Xcode's StoreKit testing, Google's license testers.
SANDBOX = "sandbox"


class StoreUnreachable(Exception):
    """The store could not be asked just now. Nothing was decided; ask again later.

    Never shown to a student verbatim. It can carry a store's raw error, and "we could
    not reach Google Play just now" is the whole of what a person needs.
    """


class ProofRejected(Exception):
    """The store says this is not a genuine purchase of ours, or it could not be checked.

    A bad signature, a token Google has never issued, a transaction for another app, a
    push notification without Google's signature. Final: asking again will not change it.
    """


@dataclass(frozen=True)
class StorePurchase:
    """One purchase, as its store describes it."""

    store: str
    #: Google's purchase token or Apple's transaction id: the store's handle on it.
    ref: str
    store_product_id: str
    state: str
    environment: str
    #: The private id the app attached at purchase (`users.store_account_token`), or
    #: None for a purchase made without one.
    account_token: str | None
    order_id: str | None = None
    purchased_at: datetime | None = None
    #: Google only: already consumed, so there is nothing left to do at Google.
    consumed: bool = False
    price_micros: int | None = None
    currency: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VoidedPurchase:
    """A Google purchase that has been refunded, charged back or revoked."""

    purchase_token: str
    order_id: str | None
    voided_at: datetime | None


@dataclass(frozen=True)
class AppleNotification:
    """An App Store Server Notification, after its signature has been checked."""

    #: Apple's own word: `ONE_TIME_CHARGE`, `REFUND`, `REFUND_REVERSED`, `TEST`, ...
    kind: str
    subtype: str | None
    uuid: str | None
    #: The transaction it is about, verified in its own right. None for a notification
    #: that names no transaction, such as `TEST`.
    purchase: StorePurchase | None


@runtime_checkable
class GooglePlay(Protocol):
    """What the backend needs from Google Play. Nothing more."""

    package_name: str

    async def get_purchase(self, purchase_token: str) -> StorePurchase:
        """Google's own record of a one-time purchase."""
        ...

    async def consume(self, store_product_id: str, purchase_token: str) -> None:
        """Tell Google the purchase has been granted.

        Consuming also acknowledges, and Google refunds any purchase that is not
        acknowledged within three days. It is also what lets the same pass be bought
        again.
        """
        ...

    async def voided_since(self, since: datetime) -> list[VoidedPurchase]:
        """Every one-time purchase voided since `since` (Google keeps 30 days)."""
        ...

    async def verify_push(self, authorization: str) -> None:
        """Raise [ProofRejected] unless a Pub/Sub push carries Google's token for us."""
        ...


@runtime_checkable
class AppStore(Protocol):
    """What the backend needs from the App Store. Nothing more."""

    async def verify_transaction(self, signed_transaction: str) -> StorePurchase:
        """Check a signed transaction against Apple's certificates and read it."""
        ...

    async def verify_notification(self, signed_payload: str) -> AppleNotification:
        """Check a server notification, and the transaction inside it."""
        ...

    async def notification_history(self, since: datetime) -> list[str]:
        """Signed payloads of notifications Apple could not deliver since `since`."""
        ...
