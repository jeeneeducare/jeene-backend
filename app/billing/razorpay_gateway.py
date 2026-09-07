"""Razorpay, behind the gateway port.

Talks to the REST API over `httpx` rather than through Razorpay's SDK. The SDK is
synchronous, and one blocking call inside an async request handler stalls the whole
event loop for the duration of a network round trip — which on a checkout endpoint is
exactly when the server is busiest. Three endpoints and HTTP Basic auth is not enough
surface to justify that.

The key secret does two jobs and never leaves this process for either: it authenticates
these calls, and it is the HMAC key that proves a success callback or a webhook really
came from Razorpay.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.billing.gateway import (
    GatewayError,
    GatewayOrder,
    GatewayPayment,
    constant_time_equals,
    hmac_sha256_hex,
)

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.razorpay.com/v1"

#: A student is waiting behind this call, so it fails rather than hangs. The reconciler
#: is what covers a timeout: a pending row with no local record of the outcome is exactly
#: the case it exists to sweep up.
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class RazorpayGateway:
    """The live gateway. Constructed once at startup and shared."""

    def __init__(self, *, key_id: str, key_secret: str, webhook_secret: str = "") -> None:
        if not key_id or not key_secret:
            raise ValueError("Razorpay needs both a key id and a key secret")
        self._key_id = key_id
        self._key_secret = key_secret
        self._webhook_secret = webhook_secret

    # --- HTTP ------------------------------------------------------------------------

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.request(
                    method,
                    f"{_BASE_URL}{path}",
                    auth=(self._key_id, self._key_secret),
                    **kwargs,
                )
        except httpx.HTTPError as exc:
            # Deliberately not including the exception text: it can carry the request
            # URL, and the URL carries an order id.
            raise GatewayError(f"could not reach the gateway ({method} {path})") from exc

        if response.status_code >= 400:
            # Razorpay's error body names the field it disliked, which is useful in a log
            # and never useful to a student.
            logger.warning(
                "razorpay %s %s -> %s: %s",
                method, path, response.status_code, response.text[:400],
            )
            raise GatewayError(f"gateway refused the request ({response.status_code})")

        return response.json()

    # --- the port --------------------------------------------------------------------

    async def create_order(
        self, *, amount_paise: int, currency: str, receipt: str, notes: dict[str, str]
    ) -> GatewayOrder:
        """Register the intent to charge.

        Called *before* the local row is written. If this process dies between the two,
        what is left behind is an unpaid Razorpay order, which Razorpay expires by
        itself. The other ordering would leave a local row claiming a payment is pending
        against an order that does not exist anywhere.
        """
        body = await self._request(
            "POST",
            "/orders",
            json={
                "amount": amount_paise,
                "currency": currency,
                "receipt": receipt,
                # Echoed back on every webhook, so a payload can be traced to a student
                # without trusting anything the client said.
                "notes": notes,
                # Capture immediately. An authorised-but-uncaptured payment is money the
                # student has parted with and we have not taken, and it auto-voids days
                # later — which is a support ticket, not a state to design around.
                "payment_capture": 1,
            },
        )
        return GatewayOrder(
            order_id=body["id"],
            amount_paise=int(body["amount"]),
            currency=body["currency"],
        )

    async def fetch_payment(self, payment_id: str) -> GatewayPayment:
        return self._as_payment(await self._request("GET", f"/payments/{payment_id}"))

    async def fetch_order_payments(self, order_id: str) -> list[GatewayPayment]:
        body = await self._request("GET", f"/orders/{order_id}/payments")
        return [self._as_payment(item) for item in body.get("items", [])]

    def verify_payment_signature(
        self, *, order_id: str, payment_id: str, signature: str
    ) -> bool:
        """Whether the app's success callback was signed by Razorpay.

        Proves the callback is genuine. Does **not** prove money moved — an authorised
        payment that was never captured signs exactly the same. The settlement path
        follows this with `fetch_payment` for that reason.
        """
        if not signature:
            return False
        expected = hmac_sha256_hex(self._key_secret, f"{order_id}|{payment_id}".encode())
        return constant_time_equals(expected, signature)

    def verify_webhook_signature(self, *, body: bytes, signature: str) -> bool:
        if not signature or not self._webhook_secret:
            return False
        expected = hmac_sha256_hex(self._webhook_secret, body)
        return constant_time_equals(expected, signature)

    # --- shaping ---------------------------------------------------------------------

    @staticmethod
    def _as_payment(item: dict[str, Any]) -> GatewayPayment:
        return GatewayPayment(
            payment_id=item["id"],
            order_id=item.get("order_id", ""),
            status=item.get("status", ""),
            amount_paise=int(item.get("amount", 0)),
            # Razorpay's `error_description` is written for a person and is the only part
            # of a failure worth repeating back to one.
            failure_reason=item.get("error_description") or "",
            raw=item,
        )
