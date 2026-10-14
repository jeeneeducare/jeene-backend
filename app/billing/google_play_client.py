"""Google Play, reached over its Developer API.

Three calls and one check, all over HTTPS with a service account's token:

  * **read a one-time purchase** — `purchases.productsv2`, the record that says what was
    bought, by which account, and whether it is paid, pending or cancelled;
  * **consume it** — after the grant, never before. Consuming acknowledges, and Google
    refunds anything left unacknowledged for three days; it is also what lets the same
    pass be bought again;
  * **list voided purchases** — refunds, chargebacks and revocations, for the reconciler;
  * **check a Pub/Sub push** — the notifications route has no user and no shared secret,
    so a token Google signed for exactly our service account and audience is the whole
    of its authentication.

The service-account library is synchronous, so the token refresh runs in a worker thread;
every request itself goes out through `httpx`, which is not.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable

import httpx

from app.billing.stores import (
    CANCELLED,
    GOOGLE,
    PENDING,
    PRODUCTION,
    PURCHASED,
    REVOKED,
    SANDBOX,
    ProofRejected,
    StorePurchase,
    StoreUnreachable,
    VoidedPurchase,
)

logger = logging.getLogger(__name__)

_API = "https://androidpublisher.googleapis.com/androidpublisher/v3/applications"
_SCOPE = "https://www.googleapis.com/auth/androidpublisher"

#: Generous for one small JSON answer, and still finite: a request handler waiting on
#: Google holds a pooled database connection the whole time.
_TIMEOUT_SECONDS = 15.0

_STATES = {
    "PURCHASED": PURCHASED,
    "PENDING": PENDING,
    "CANCELLED": CANCELLED,
}


def _parse_time(value: str | None) -> datetime | None:
    """An RFC 3339 timestamp from Google, or None for anything that is not one."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _from_millis(value: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return None


def purchase_from_google(purchase_token: str, data: dict[str, Any]) -> StorePurchase:
    """Google's `ProductPurchaseV2`, in our words.

    Separate from the client so the mapping is tested against the exact JSON Google
    sends, without a network in the way.

    A purchase in a state this code does not know is read as pending: it grants nothing,
    and it fails nothing either, so the worst case is a student waiting for the
    reconciler rather than a student paid for twice or given something free.
    """
    lines = data.get("productLineItem") or []
    line = lines[0] if lines and isinstance(lines[0], dict) else {}
    offer = line.get("productOfferDetails") or {}
    state = _STATES.get(
        (data.get("purchaseStateContext") or {}).get("purchaseState", ""), PENDING
    )
    # Fully refunded leaves nothing refundable. Only an explicit zero counts: a field
    # Google left out is not evidence of a refund.
    if state == PURCHASED and offer.get("refundableQuantity") == 0:
        state = REVOKED

    return StorePurchase(
        store=GOOGLE,
        ref=purchase_token,
        store_product_id=line.get("productId") or "",
        state=state,
        environment=SANDBOX if data.get("testPurchaseContext") else PRODUCTION,
        account_token=data.get("obfuscatedExternalAccountId") or None,
        order_id=data.get("orderId") or None,
        purchased_at=_parse_time(data.get("purchaseCompletionTime")),
        consumed=offer.get("consumptionState") == "CONSUMPTION_STATE_CONSUMED",
        raw=data,
    )


class GooglePlayClient:
    """The real Google Play. Constructed once, lazily, by `app.billing.google_play()`."""

    def __init__(
        self,
        *,
        package_name: str,
        service_account_json: str,
        push_service_account: str | None,
        push_audience: str | None,
        transport: httpx.AsyncBaseTransport | None = None,
        token_verifier: Callable[[str, str], dict] | None = None,
        credentials: Any = None,
    ) -> None:
        self.package_name = package_name
        self._push_account = push_service_account
        self._push_audience = push_audience
        # Tests swap these three for fakes; nothing else should.
        self._transport = transport
        self._token_verifier = token_verifier or _verify_google_token
        self._credentials = credentials or _service_account(service_account_json)
        self._refresh_lock = asyncio.Lock()

    # --- the three calls -------------------------------------------------------------

    async def get_purchase(self, purchase_token: str) -> StorePurchase:
        url = f"{_API}/{self.package_name}/purchases/productsv2/tokens/{purchase_token}"
        data = await self._call("GET", url)
        return purchase_from_google(purchase_token, data)

    async def consume(self, store_product_id: str, purchase_token: str) -> None:
        url = (f"{_API}/{self.package_name}/purchases/products/{store_product_id}"
               f"/tokens/{purchase_token}:consume")
        try:
            await self._call("POST", url)
        except ProofRejected:
            # Google answers a second consume with an error rather than a no-op. Which
            # error it is matters less than what is true now, so ask: consumed is
            # consumed, whoever got there first.
            current = await self.get_purchase(purchase_token)
            if not current.consumed:
                raise StoreUnreachable("Google refused to consume a purchase") from None

    async def voided_since(self, since: datetime) -> list[VoidedPurchase]:
        url = f"{_API}/{self.package_name}/purchases/voidedpurchases"
        params: dict[str, Any] = {
            "startTime": int(since.timestamp() * 1000),
            # In-app purchases only. Subscriptions are not sold.
            "type": 0,
        }
        voided: list[VoidedPurchase] = []
        # Paged by token. Ten pages is far beyond any real week of refunds and still a
        # bound, so a misbehaving answer cannot keep a sweep looping.
        for _ in range(10):
            data = await self._call("GET", url, params=params)
            for item in data.get("voidedPurchases") or []:
                token = item.get("purchaseToken")
                if token:
                    voided.append(VoidedPurchase(
                        purchase_token=token,
                        order_id=item.get("orderId"),
                        voided_at=_from_millis(item.get("voidedTimeMillis")),
                    ))
            page = (data.get("tokenPagination") or {}).get("nextPageToken")
            if not page:
                break
            params = {"token": page}
        return voided

    # --- the check -------------------------------------------------------------------

    async def verify_push(self, authorization: str) -> None:
        """Refuse a push that does not carry Google's token for our subscription.

        Pub/Sub signs an OpenID token as the service account chosen on the subscription,
        for the audience chosen there. Both have to match what this deployment was told,
        and Google has to say the email is verified: a token minted by some other Google
        account for our URL proves only that somebody can read our URL.
        """
        if not self._push_account or not self._push_audience:
            raise ProofRejected("push authentication is not configured")
        if not authorization.lower().startswith("bearer "):
            raise ProofRejected("no bearer token")
        token = authorization.split(" ", 1)[1].strip()
        try:
            claims = await asyncio.to_thread(self._token_verifier, token, self._push_audience)
        except Exception as exc:  # noqa: BLE001 — any failure to verify is a refusal
            raise ProofRejected("push token did not verify") from exc
        if claims.get("email") != self._push_account or not claims.get("email_verified"):
            raise ProofRejected("push token is not from our subscription")

    # --- plumbing --------------------------------------------------------------------

    async def _token(self) -> str:
        async with self._refresh_lock:
            if not self._credentials.valid:
                try:
                    await asyncio.to_thread(_refresh, self._credentials)
                except Exception as exc:  # noqa: BLE001
                    logger.error("could not get a Google Play API token")
                    raise StoreUnreachable("could not authenticate with Google") from exc
            return self._credentials.token

    async def _call(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        """One request. Maps every failure onto the two answers callers act on.

        **Retry later** ([StoreUnreachable]): the network, Google's 5xx and 429, and our own
        credentials being refused. A 401 or 403 is a configuration fault, not the
        purchase's, so it must never end a purchase as invalid.

        **Final** ([ProofRejected]): any other 4xx. Google answering "no such token" to a
        token the app sent means the token is not a purchase of ours.
        """
        token = await self._token()
        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT_SECONDS, transport=self._transport
            ) as client:
                response = await client.request(
                    method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs
                )
        except httpx.HTTPError as exc:
            raise StoreUnreachable("Google Play could not be reached") from exc

        if response.status_code in (401, 403):
            logger.error("Google Play refused our credentials: %s", response.status_code)
            raise StoreUnreachable("Google Play refused our credentials")
        if response.status_code == 429 or response.status_code >= 500:
            raise StoreUnreachable(f"Google Play answered {response.status_code}")
        if response.status_code >= 400:
            raise ProofRejected(f"Google Play answered {response.status_code}")
        if not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise StoreUnreachable("Google Play sent something that is not JSON") from exc
        return data if isinstance(data, dict) else {}


def _service_account(service_account_json: str) -> Any:
    from google.oauth2 import service_account

    try:
        info = json.loads(service_account_json)
    except ValueError as exc:
        # Never echo the value: it is a private key.
        raise ValueError("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
    return service_account.Credentials.from_service_account_info(info, scopes=[_SCOPE])


def _refresh(credentials: Any) -> None:
    from google.auth.transport.requests import Request

    credentials.refresh(Request())


def _verify_google_token(token: str, audience: str) -> dict:
    """Google's own verification of an OpenID token: signature, expiry, issuer, audience."""
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    return id_token.verify_oauth2_token(token, Request(), audience=audience)
