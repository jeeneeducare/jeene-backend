"""Billing: what Pro costs, who has it, and how money gets taken.

The gateway is constructed once, lazily, and only when `JEENE_BILLING_ENABLED` is set
with a key pair beside it. A deployment that is half-configured therefore refuses to take
payments rather than taking them badly — the money routes answer 503 and say so, instead
of failing somewhere deeper with a student's card details already in flight.
"""

from __future__ import annotations

import logging

from app.billing.gateway import PaymentGateway
from app.config import settings

logger = logging.getLogger(__name__)

_gateway: PaymentGateway | None = None


class BillingUnavailable(Exception):
    """Payments are not configured on this deployment."""


def gateway() -> PaymentGateway:
    """The configured gateway, or raise [BillingUnavailable].

    Lazy rather than built at import, so the test suite and any deployment that does not
    sell anything never needs Razorpay credentials to start.
    """
    global _gateway
    if _gateway is not None:
        return _gateway

    if not settings.jeene_billing_enabled:
        raise BillingUnavailable("billing is switched off on this deployment")
    if not settings.razorpay_key_id or not settings.razorpay_key_secret:
        raise BillingUnavailable("billing is switched on but has no gateway credentials")

    from app.billing.razorpay_gateway import RazorpayGateway

    _gateway = RazorpayGateway(
        key_id=settings.razorpay_key_id,
        key_secret=settings.razorpay_key_secret,
        webhook_secret=settings.razorpay_webhook_secret or "",
    )
    logger.info("billing enabled: razorpay gateway ready")
    return _gateway


def set_gateway(replacement: PaymentGateway | None) -> None:
    """Swap the gateway. Tests only — there is no other caller and there should not be."""
    global _gateway
    _gateway = replacement
