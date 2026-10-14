"""Billing: what Pro costs, who has it, and how a store purchase becomes access.

Each store's client is constructed once, lazily, and only when `JEENE_BILLING_ENABLED` is
set with that store's credentials beside it. A deployment that is half-configured
therefore refuses rather than half-works: a store route answers 503 and says so, instead
of failing somewhere deeper with a student's purchase in flight.

The two stores are configured independently. One can be live while the other is still
being set up, and a store with nothing configured simply says it is not available.
"""

from __future__ import annotations

import logging

from app.billing.stores import AppStore, GooglePlay
from app.config import settings

logger = logging.getLogger(__name__)

_google_play: GooglePlay | None = None
_app_store: AppStore | None = None


class BillingUnavailable(Exception):
    """This store is not configured on this deployment, or billing is switched off."""


def google_play() -> GooglePlay:
    """The configured Google Play client, or raise [BillingUnavailable].

    Lazy rather than built at import, so the test suite and any deployment that does not
    sell anything never needs a service account to start.
    """
    global _google_play
    if _google_play is not None:
        return _google_play

    if not settings.jeene_billing_enabled:
        raise BillingUnavailable("billing is switched off on this deployment")
    if not settings.google_play_package_name or not settings.google_play_service_account_json:
        raise BillingUnavailable("Google Play billing is not configured")

    from app.billing.google_play_client import GooglePlayClient

    _google_play = GooglePlayClient(
        package_name=settings.google_play_package_name,
        service_account_json=settings.google_play_service_account_json,
        push_service_account=settings.google_pubsub_push_service_account,
        push_audience=settings.google_pubsub_push_audience,
    )
    logger.info("billing: Google Play ready")
    return _google_play


def app_store() -> AppStore:
    """The configured App Store client, or raise [BillingUnavailable]."""
    global _app_store
    if _app_store is not None:
        return _app_store

    if not settings.jeene_billing_enabled:
        raise BillingUnavailable("billing is switched off on this deployment")
    if not settings.apple_bundle_id:
        raise BillingUnavailable("App Store billing is not configured")

    from app.billing.app_store_client import AppleAppStore, apple_root_certificates

    _app_store = AppleAppStore(
        bundle_id=settings.apple_bundle_id,
        app_apple_id=settings.apple_app_id,
        root_certificates=apple_root_certificates(),
        key_id=settings.apple_iap_key_id,
        issuer_id=settings.apple_iap_issuer_id,
        private_key=settings.apple_iap_private_key,
        accept_xcode=settings.apple_accept_xcode_transactions,
    )
    logger.info("billing: App Store ready (production %s)",
                "on" if settings.apple_app_id else "off: APPLE_APP_ID is not set")
    return _app_store


def set_google_play(replacement: GooglePlay | None) -> None:
    """Swap the Google Play client. Tests only — there is no other caller and there should not be."""
    global _google_play
    _google_play = replacement


def set_app_store(replacement: AppStore | None) -> None:
    """Swap the App Store client. Tests only."""
    global _app_store
    _app_store = replacement
