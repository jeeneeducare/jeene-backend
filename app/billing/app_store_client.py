"""The App Store, checked with Apple's own server library.

On iPhone the app hands over a **signed transaction**: a JWS that Apple signed with a
certificate chaining up to Apple Root CA - G3. Checking that chain, the signature, the
bundle id and the environment is the whole of the proof that a purchase is real, and it
is done here with `app-store-server-library`, Apple's official implementation, rather
than by hand. The root certificate it trusts is committed under `apple_roots/`; it is
public, and pinning it is the point.

Three environments arrive and each is verified on its own terms:

  * **Production** — a real purchase. Apple's library will not verify one without the
    app's numeric Apple ID, so a deployment missing `APPLE_APP_ID` cannot accept them.
  * **Sandbox** — TestFlight, sandbox testers, and Apple's own reviewers, who buy this
    way against the live backend. Accepted, and recorded as sandbox so revenue never
    counts them.
  * **Xcode** — StoreKit testing on a laptop, signed by a certificate Apple never
    issued. Accepted only when `APPLE_ACCEPT_XCODE_TRANSACTIONS` is set, which no
    deployment does: there is nothing to verify, so accepting one is believing it.

The library verifies synchronously, and with online checks on it asks Apple's OCSP
responder whether a certificate has been revoked, so every verification runs in a worker
thread. The App Store Server API — only the reconciler uses it — has an async client.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import jwt
from appstoreserverlibrary.models.Environment import Environment
from appstoreserverlibrary.signed_data_verifier import (
    SignedDataVerifier,
    VerificationException,
    VerificationStatus,
)

from app.billing.stores import (
    APPLE,
    PRODUCTION,
    PURCHASED,
    REVOKED,
    SANDBOX,
    AppleNotification,
    ProofRejected,
    StorePurchase,
    StoreUnreachable,
)

logger = logging.getLogger(__name__)

_ROOTS = Path(__file__).parent / "apple_roots"

#: A ceiling on one sweep's history pages. A day of missed notifications is a page or
#: two; the bound only exists so a misbehaving answer cannot loop the reconciler.
_HISTORY_PAGES = 10


def apple_root_certificates() -> list[bytes]:
    """Apple's root certificates, as committed. The only thing trusted to start a chain."""
    return [path.read_bytes() for path in sorted(_ROOTS.glob("*.cer"))]


def _claims(signed: str) -> dict[str, Any]:
    """What a JWS says about itself, before anything has checked it.

    Used for one decision only: which environment's verifier to hand it to. Every value
    that matters is read again from the verified result.
    """
    try:
        claims = jwt.decode(signed, options={"verify_signature": False})
    except jwt.PyJWTError as exc:
        raise ProofRejected("not a signed App Store object") from exc
    return claims if isinstance(claims, dict) else {}


def _from_millis(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def _account_token(value: Any) -> str | None:
    """The app account token, normalised. Anything that is not a UUID is no token at all."""
    if not value:
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError:
        return None


class AppleAppStore:
    """The real App Store. Constructed once, lazily, by `app.billing.app_store()`."""

    def __init__(
        self,
        *,
        bundle_id: str,
        app_apple_id: int | None,
        root_certificates: list[bytes],
        key_id: str | None = None,
        issuer_id: str | None = None,
        private_key: str | None = None,
        accept_xcode: bool = False,
        online_checks: bool = True,
    ) -> None:
        self._bundle_id = bundle_id
        self._app_apple_id = app_apple_id
        self._verifiers: dict[str, SignedDataVerifier] = {
            Environment.SANDBOX.value: SignedDataVerifier(
                root_certificates, online_checks, Environment.SANDBOX, bundle_id, app_apple_id
            ),
        }
        if app_apple_id:
            self._verifiers[Environment.PRODUCTION.value] = SignedDataVerifier(
                root_certificates, online_checks, Environment.PRODUCTION, bundle_id,
                app_apple_id,
            )
        if accept_xcode:
            logger.warning("accepting Xcode-signed transactions: local development only")
            self._verifiers[Environment.XCODE.value] = SignedDataVerifier(
                [], False, Environment.XCODE, bundle_id
            )
        self._api_key = (key_id, issuer_id, private_key) if (
            key_id and issuer_id and private_key) else None

    def _verifier_for(self, environment: str | None) -> SignedDataVerifier:
        verifier = self._verifiers.get(environment or "")
        if verifier is not None:
            return verifier
        if environment == Environment.PRODUCTION.value:
            # A real purchase this deployment cannot check yet. Not the purchase's fault,
            # so it is not refused for ever: the app keeps it and asks again.
            logger.error("a production transaction arrived but APPLE_APP_ID is not set")
            raise StoreUnreachable("production transactions need APPLE_APP_ID")
        raise ProofRejected(f"transactions from {environment!r} are not accepted")

    async def _verify(self, method_name: str, signed: str, environment: str | None) -> Any:
        verifier = self._verifier_for(environment)
        try:
            return await asyncio.to_thread(getattr(verifier, method_name), signed)
        except VerificationException as exc:
            if exc.status == VerificationStatus.RETRYABLE_VERIFICATION_FAILURE:
                raise StoreUnreachable("Apple's certificate check could not be completed") from exc
            raise ProofRejected(f"Apple's signature did not verify: {exc.status.name}") from exc

    async def verify_transaction(self, signed_transaction: str) -> StorePurchase:
        claims = _claims(signed_transaction)
        environment = claims.get("environment")
        decoded = await self._verify(
            "verify_and_decode_signed_transaction", signed_transaction, environment
        )
        return StorePurchase(
            store=APPLE,
            ref=decoded.transactionId or "",
            store_product_id=decoded.productId or "",
            # A refunded or revoked transaction carries its revocation date, and is never
            # a reason to grant anything however good its signature.
            state=REVOKED if decoded.revocationDate else PURCHASED,
            environment=PRODUCTION if environment == Environment.PRODUCTION.value else SANDBOX,
            account_token=_account_token(decoded.appAccountToken),
            order_id=decoded.originalTransactionId,
            purchased_at=_from_millis(decoded.purchaseDate),
            # Apple reports the price in milliunits: ₹199 is 199000.
            price_micros=decoded.price * 1000 if decoded.price is not None else None,
            currency=decoded.currency,
            raw=claims,
        )

    async def verify_notification(self, signed_payload: str) -> AppleNotification:
        claims = _claims(signed_payload)
        data = claims.get("data") or claims.get("summary") or {}
        environment = data.get("environment") if isinstance(data, dict) else None
        decoded = await self._verify("verify_and_decode_notification", signed_payload, environment)

        purchase = None
        if decoded.data and decoded.data.signedTransactionInfo:
            # Signed in its own right, and checked in its own right: a notification's
            # signature vouches for the envelope, and the transaction inside is the part
            # that grants anything.
            purchase = await self.verify_transaction(decoded.data.signedTransactionInfo)
        return AppleNotification(
            kind=decoded.rawNotificationType or "",
            subtype=decoded.rawSubtype,
            uuid=decoded.notificationUUID,
            purchase=purchase,
        )

    async def notification_history(self, since: datetime) -> list[str]:
        """Notifications Apple tried to send since `since` and could not deliver.

        `onlyFailures` keeps this to what the route never saw, so a sweep re-reads days
        of history without re-processing everything that already arrived — though doing
        so would be harmless, every path being idempotent.
        """
        if self._api_key is None:
            return []
        from appstoreserverlibrary.api_client import (
            APIException,
            AsyncAppStoreServerAPIClient,
        )
        from appstoreserverlibrary.models.NotificationHistoryRequest import (
            NotificationHistoryRequest,
        )

        key_id, issuer_id, private_key = self._api_key
        start = int(since.timestamp() * 1000)
        end = int(datetime.now(timezone.utc).timestamp() * 1000)
        payloads: list[str] = []
        environments = [Environment.SANDBOX]
        if self._app_apple_id:
            environments.insert(0, Environment.PRODUCTION)

        for environment in environments:
            client = AsyncAppStoreServerAPIClient(
                private_key.encode(), key_id, issuer_id, self._bundle_id, environment
            )
            try:
                request = NotificationHistoryRequest(
                    startDate=start, endDate=end, onlyFailures=True
                )
                page_token = None
                for _ in range(_HISTORY_PAGES):
                    response = await client.get_notification_history(page_token, request)
                    payloads.extend(
                        item.signedPayload for item in response.notificationHistory or []
                        if item.signedPayload
                    )
                    if not response.hasMore or not response.paginationToken:
                        break
                    page_token = response.paginationToken
            except APIException as exc:
                raise StoreUnreachable(
                    f"App Store Server API answered {exc.http_status_code}"
                ) from exc
            except Exception as exc:  # noqa: BLE001 — the network, a bad key, anything
                raise StoreUnreachable("App Store Server API could not be reached") from exc
            finally:
                await client.async_close()
        return payloads
