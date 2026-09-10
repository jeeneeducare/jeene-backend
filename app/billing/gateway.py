"""The payment gateway, behind a port.

Razorpay is reached through a Protocol for the same reason the planner reaches OpenAI
through one: so the code that decides things can be tested without the network, and so
the day a second provider appears is a day of adding a class.

The port is deliberately narrow. Creating an order and asking what really happened to a
payment are the only two things the backend needs from a gateway; everything else —
whether to grant access, what a price is, when to give up on a pending row — is ours and
stays here.

Two rules live in this file rather than at the call site, because both are easy to get
wrong once and never notice:

**Signature verification is constant-time.** A comparison that returns on the first
differing byte leaks the expected digest a byte at a time, and that digest is a forged
success callback.

**A capture is only a capture if the gateway says so.** `verify_signature` proves the
callback came from Razorpay; it does not prove money moved. Only `fetch_payment` does,
and the settlement path calls both.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from app.security import constant_time_equals


class GatewayError(Exception):
    """The gateway could not be reached, or answered something we cannot act on.

    Never surfaced to a student verbatim: it may carry account identifiers, and "we could
    not reach the bank" is the whole of what a person needs.
    """


@dataclass(frozen=True)
class GatewayOrder:
    """An order the gateway has accepted and is waiting to be paid."""

    order_id: str
    amount_paise: int
    currency: str


@dataclass(frozen=True)
class GatewayPayment:
    """What the gateway says actually happened to a payment.

    `status` is Razorpay's own word — `created`, `authorized`, `captured`, `failed`,
    `refunded`. Not translated here: the settlement path maps it to our states in one
    place, and a mapping done in two places is a mapping that disagrees with itself.
    """

    payment_id: str
    order_id: str
    status: str
    amount_paise: int
    failure_reason: str = ""
    raw: dict[str, Any] | None = None

    @property
    def captured(self) -> bool:
        return self.status == "captured"

    @property
    def dead(self) -> bool:
        """Terminal and unpaid. `created` and `authorized` are neither — still in flight."""
        return self.status in ("failed", "refunded")

    @property
    def in_flight(self) -> bool:
        """Neither paid nor finished. Somebody may still be typing an OTP.

        The reason failing a payment is never decided by elapsed time alone: an attempt in
        this state is a student mid-purchase, and the slower their connection the longer
        they sit here.
        """
        return not self.captured and not self.dead


@runtime_checkable
class PaymentGateway(Protocol):
    """What the backend needs from a payment gateway. Nothing more."""

    async def create_order(
        self, *, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> GatewayOrder:
        """Register an intent to charge. Called before anything is written locally."""
        ...

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        """What really happened. The only thing that proves money moved."""
        ...

    async def fetch_order_payments(self, order_id: str) -> list[GatewayPayment]:
        """Every payment attempted against an order, for the reconciler."""
        ...

    def verify_payment_signature(
        self, *, order_id: str, payment_id: str, signature: str
    ) -> bool:
        """Whether the client's success callback was really signed by the gateway."""
        ...

    def verify_webhook_signature(self, *, body: bytes, signature: str) -> bool:
        """Whether a webhook body was really signed by the gateway.

        Takes raw bytes, never a parsed object: re-serialising JSON changes whitespace
        and key order, and the signature is over what was sent, not over what it meant.
        """
        ...


def hmac_sha256_hex(secret: str, message: bytes) -> str:
    return hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


# `constant_time_equals` is imported above rather than defined here. It was defined here
# once, and then a browser sitting a test and the notes viewer each grew their own copy —
# both with the crash it had at the time. One implementation, in `app.security`.
