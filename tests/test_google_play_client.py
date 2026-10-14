"""The real Google Play client, with only the network replaced.

Every request goes through `httpx.MockTransport`, so what is tested is the code that
builds Google's URLs, reads Google's JSON and decides what a failure means — the mapping
the rest of billing trusts. The answers are shaped like the `ProductPurchaseV2` and
voided-purchase records in Google's API reference.

What matters most is the split between the two kinds of failure. A purchase Google says
does not exist is final; Google being down, or refusing *our* credentials, is a reason to
try again later — and must never end a student's purchase as invalid.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from app.billing.google_play_client import GooglePlayClient, purchase_from_google
from app.billing.stores import (
    CANCELLED,
    GOOGLE,
    PENDING,
    PRODUCTION,
    PURCHASED,
    REVOKED,
    SANDBOX,
    ProofRejected,
    StoreUnreachable,
)

PACKAGE = "com.jeene.test"
PUSH_ACCOUNT = "jeene-play-billing@jeene.iam.gserviceaccount.com"
AUDIENCE = "https://jeene.test/billing/google/notifications"


class Credentials:
    """A service account that is always signed in. Refreshing is google-auth's job."""

    valid = True
    token = "test-access-token"


def _purchase_json(state="PURCHASED", consumption="CONSUMPTION_STATE_YET_TO_BE_CONSUMED",
                   refundable=1, test=False, account="6b0d6a3e-2f0c-4a53-9d3e-1f2a3b4c5d6e"):
    data = {
        "kind": "androidpublisher#productPurchaseV2",
        "orderId": "GPA.3301-1234-5678-90123",
        "obfuscatedExternalAccountId": account,
        "purchaseCompletionTime": "2026-10-14T10:15:30.123Z",
        "regionCode": "IN",
        "acknowledgementState": "ACKNOWLEDGEMENT_STATE_PENDING",
        "purchaseStateContext": {"purchaseState": state},
        "productLineItem": [{
            "productId": "pro_pass_30d",
            "productOfferDetails": {
                "purchaseOptionId": "buy",
                "quantity": 1,
                "refundableQuantity": refundable,
                "consumptionState": consumption,
            },
        }],
    }
    if test:
        data["testPurchaseContext"] = {"fopType": "TEST"}
    return data


def _client(handler, **kwargs) -> GooglePlayClient:
    options = {
        "package_name": PACKAGE,
        "service_account_json": "{}",
        "push_service_account": PUSH_ACCOUNT,
        "push_audience": AUDIENCE,
        "transport": httpx.MockTransport(handler),
        "credentials": Credentials(),
    }
    options.update(kwargs)
    return GooglePlayClient(**options)


# --- reading Google's record ---------------------------------------------------------------


def test_a_paid_purchase_is_read_into_our_shape():
    purchase = purchase_from_google("tok", _purchase_json())

    assert purchase.store == GOOGLE
    assert purchase.ref == "tok"
    assert purchase.store_product_id == "pro_pass_30d"
    assert purchase.state == PURCHASED
    assert purchase.environment == PRODUCTION
    assert purchase.account_token == "6b0d6a3e-2f0c-4a53-9d3e-1f2a3b4c5d6e"
    assert purchase.order_id == "GPA.3301-1234-5678-90123"
    assert purchase.purchased_at == datetime(2026, 10, 14, 10, 15, 30, 123000,
                                             tzinfo=timezone.utc)
    assert purchase.consumed is False


@pytest.mark.parametrize("google_says, ours", [
    ("PENDING", PENDING),
    ("CANCELLED", CANCELLED),
    # A state Google adds tomorrow grants nothing and fails nothing.
    ("PURCHASE_STATE_UNSPECIFIED", PENDING),
    ("SOMETHING_NEW", PENDING),
])
def test_every_state_google_can_send_maps_onto_ours(google_says, ours):
    assert purchase_from_google("tok", _purchase_json(state=google_says)).state == ours


def test_a_fully_refunded_purchase_is_revoked_but_a_missing_field_proves_nothing():
    assert purchase_from_google("tok", _purchase_json(refundable=0)).state == REVOKED
    data = _purchase_json()
    del data["productLineItem"][0]["productOfferDetails"]["refundableQuantity"]
    assert purchase_from_google("tok", data).state == PURCHASED


def test_a_license_testers_purchase_is_sandbox_and_a_consumed_one_says_so():
    purchase = purchase_from_google(
        "tok", _purchase_json(test=True, consumption="CONSUMPTION_STATE_CONSUMED"))
    assert purchase.environment == SANDBOX
    assert purchase.consumed is True


def test_the_client_asks_for_the_right_record_with_our_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json=_purchase_json())

    purchase = asyncio.run(_client(handler).get_purchase("tok-123"))

    assert seen["url"] == ("https://androidpublisher.googleapis.com/androidpublisher/v3/"
                           f"applications/{PACKAGE}/purchases/productsv2/tokens/tok-123")
    assert seen["auth"] == "Bearer test-access-token"
    assert purchase.state == PURCHASED


@pytest.mark.parametrize("status, error", [
    (400, ProofRejected), (404, ProofRejected), (410, ProofRejected),
    # Our credentials being refused is our fault, not the purchase's.
    (401, StoreUnreachable), (403, StoreUnreachable),
    (429, StoreUnreachable), (500, StoreUnreachable), (503, StoreUnreachable),
])
def test_each_failure_is_either_final_or_try_again(status, error):
    client = _client(lambda request: httpx.Response(status, json={"error": {}}))
    with pytest.raises(error):
        asyncio.run(client.get_purchase("tok"))


def test_no_network_is_try_again():
    def handler(request):
        raise httpx.ConnectError("no route to host")

    with pytest.raises(StoreUnreachable):
        asyncio.run(_client(handler).get_purchase("tok"))


# --- consuming -----------------------------------------------------------------------------


def test_consuming_posts_to_the_purchase():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, str(request.url)))
        return httpx.Response(200)

    asyncio.run(_client(handler).consume("pro_pass_30d", "tok-9"))

    assert calls == [("POST", "https://androidpublisher.googleapis.com/androidpublisher/v3/"
                              f"applications/{PACKAGE}/purchases/products/pro_pass_30d/"
                              "tokens/tok-9:consume")]


def test_consuming_twice_is_fine_once_google_confirms_it_is_consumed():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(400, json={"error": {"message": "already consumed"}})
        return httpx.Response(200, json=_purchase_json(
            consumption="CONSUMPTION_STATE_CONSUMED"))

    asyncio.run(_client(handler).consume("pro_pass_30d", "tok"))


def test_a_refused_consume_that_google_says_did_not_happen_is_retried_later():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(400, json={})
        return httpx.Response(200, json=_purchase_json())

    with pytest.raises(StoreUnreachable):
        asyncio.run(_client(handler).consume("pro_pass_30d", "tok"))


# --- voided purchases ------------------------------------------------------------------------


def test_voided_purchases_are_read_across_pages():
    pages = {
        None: {"voidedPurchases": [{"purchaseToken": "a", "orderId": "GPA.a",
                                    "voidedTimeMillis": "1760400000000"}],
               "tokenPagination": {"nextPageToken": "page-2"}},
        "page-2": {"voidedPurchases": [{"purchaseToken": "b", "orderId": "GPA.b",
                                        "voidedTimeMillis": "1760400001000"}]},
    }
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("token")
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages[page])

    voided = asyncio.run(_client(handler).voided_since(datetime(2026, 10, 11,
                                                                tzinfo=timezone.utc)))

    assert [v.purchase_token for v in voided] == ["a", "b"]
    assert seen[0]["type"] == "0", "one-time purchases only"
    assert seen[0]["startTime"] == str(int(datetime(2026, 10, 11,
                                                    tzinfo=timezone.utc).timestamp() * 1000))


# --- Pub/Sub's token ---------------------------------------------------------------------------


def _verifier(claims=None, raises=False):
    def verify(token: str, audience: str) -> dict:
        assert audience == AUDIENCE, "verified for our audience, not whatever is in the token"
        if raises:
            raise ValueError("bad signature")
        return claims
    return verify


def _check(authorization: str, **kwargs):
    client = _client(lambda request: httpx.Response(500), **kwargs)
    return asyncio.run(client.verify_push(authorization))


def test_a_push_signed_for_our_subscription_is_accepted():
    _check("Bearer ok", token_verifier=_verifier({"email": PUSH_ACCOUNT,
                                                  "email_verified": True}))


@pytest.mark.parametrize("authorization, verifier", [
    ("", _verifier({"email": PUSH_ACCOUNT, "email_verified": True})),
    ("Basic abc", _verifier({"email": PUSH_ACCOUNT, "email_verified": True})),
    ("Bearer bad", _verifier(raises=True)),
    ("Bearer other", _verifier({"email": "someone@else.iam.gserviceaccount.com",
                                "email_verified": True})),
    ("Bearer unverified", _verifier({"email": PUSH_ACCOUNT, "email_verified": False})),
])
def test_anything_else_is_refused(authorization, verifier):
    with pytest.raises(ProofRejected):
        _check(authorization, token_verifier=verifier)


def test_a_deployment_not_told_who_signs_pushes_refuses_them_all():
    with pytest.raises(ProofRejected):
        _check("Bearer ok", push_service_account=None,
               token_verifier=_verifier({"email": PUSH_ACCOUNT, "email_verified": True}))


def test_importing_the_store_clients_never_replaces_the_accessors():
    """A submodule named like a package attribute replaces it on import.

    These modules were once called `google_play.py` and `app_store.py`, the same names as
    the accessors in `app.billing`. Importing either replaced `app.billing.google_play()`
    with the module, and every route that looked it up afterwards called a module: a
    "'module' object is not callable" on the first purchase after anything imported it.
    """
    import app.billing as billing
    import app.billing.app_store_client  # noqa: F401
    import app.billing.google_play_client  # noqa: F401

    assert callable(billing.google_play)
    assert callable(billing.app_store)
