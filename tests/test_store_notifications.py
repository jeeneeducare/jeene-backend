"""The stores' own notifications, and the reconciler that reads what they missed.

Two routes nobody signs in to, so what is asserted first is what each refuses:

  * **Google** — a Pub/Sub push is believed only with Google's token for our
    subscription, and a purchase it mentions is always read back from Google before it
    grants anything;
  * **Apple** — the payload, and the transaction inside it, must verify.

Then that each notification does what it says exactly once, and that the reconciler
catches the cases where the notification never came: an unconsumed purchase before
Google refunds it, a pending one that has since been paid, a refund nobody announced.
"""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.billing.stores import (
    APPLE,
    PENDING,
    PRODUCTION,
    PURCHASED,
    AppleNotification,
    ProofRejected,
    StorePurchase,
    StoreUnreachable,
    VoidedPurchase,
)
from tests.test_store_purchases import (  # noqa: F401 — fixtures shared with the purchase tests
    PASS_30,
    SKU_30,
    STRANGER,
    STRANGER_TOKEN,
    STUDENT,
    STUDENT_TOKEN,
    FakeGooglePlay,
    _grants,
    _purchase,
    _sql,
    client,
    fresh_ledger,
    google,
    pytestmark,
    world,
)

PUSH = {"Authorization": "Bearer google-signed"}


def _push(client, notification: dict, headers=PUSH):
    data = base64.b64encode(json.dumps(notification).encode()).decode()
    return client.post("/billing/google/notifications", headers=headers, json={
        "message": {"data": data, "messageId": "1"},
        "subscription": "projects/p/subscriptions/play-billing-push",
    })


def _purchased(token: str) -> dict:
    return {"packageName": FakeGooglePlay.package_name, "eventTimeMillis": "1",
            "oneTimeProductNotification": {"version": "1.0", "notificationType": 1,
                                           "purchaseToken": token, "sku": SKU_30}}


def _voided(token: str, product_type: int = 2) -> dict:
    return {"packageName": FakeGooglePlay.package_name, "eventTimeMillis": "1",
            "voidedPurchaseNotification": {"purchaseToken": token, "orderId": "GPA.1",
                                           "productType": product_type, "refundType": 1}}


class FakeAppleNotifications:
    """The App Store, answering notifications from a table of what each one says."""

    def __init__(self) -> None:
        self.notes: dict[str, AppleNotification] = {}
        self.history: list[str] = []
        self.unreachable = False

    def says(self, signed: str, kind: str, transaction_id: str | None = None,
             account: str | None = STUDENT_TOKEN) -> None:
        purchase = None
        if transaction_id:
            purchase = StorePurchase(
                store=APPLE, ref=transaction_id, store_product_id=SKU_30, state=PURCHASED,
                environment=PRODUCTION, account_token=account, order_id=transaction_id,
                purchased_at=datetime.now(timezone.utc), raw={},
            )
        self.notes[signed] = AppleNotification(kind=kind, subtype=None,
                                                uuid=str(uuid.uuid4()), purchase=purchase)

    async def verify_transaction(self, signed_transaction):
        raise ProofRejected("not used here")

    async def verify_notification(self, signed_payload):
        if self.unreachable:
            raise StoreUnreachable("OCSP down")
        if signed_payload not in self.notes:
            raise ProofRejected("did not verify")
        return self.notes[signed_payload]

    async def notification_history(self, since):
        return list(self.history)


@pytest.fixture
def apple_notes():
    from app import billing

    fake = FakeAppleNotifications()
    billing.set_app_store(fake)
    yield fake
    billing.set_app_store(None)


# --- Google ------------------------------------------------------------------------------


def test_a_push_without_googles_token_is_refused(client, google):
    google.says("tok-n1")
    assert _push(client, _purchased("tok-n1"), headers={}).status_code == 401
    assert _push(client, _purchased("tok-n1"),
                 headers={"Authorization": "Bearer forged"}).status_code == 401
    assert _purchase("google", "tok-n1") is None


def test_a_purchase_google_announces_is_read_back_granted_and_consumed(client, google):
    google.says("tok-n2")
    response = _push(client, _purchased("tok-n2"))

    assert response.status_code == 200
    assert response.json()["status"] == "granted"
    assert google.consumed == ["tok-n2"]
    assert len(_grants()) == 1


def test_the_notification_and_the_app_arriving_together_grant_once(client, google):
    google.says("tok-n3")
    _push(client, _purchased("tok-n3"))
    body = client.post("/billing/purchases", json={
        "store": "google", "product_id": SKU_30, "purchase_token": "tok-n3",
    }).json()

    assert body["status"] == "already_granted"
    assert len(_grants()) == 1


def test_a_purchase_completing_later_is_granted_with_the_app_closed(client, google):
    google.says("tok-n4", state=PENDING)
    client.post("/billing/purchases", json={
        "store": "google", "product_id": SKU_30, "purchase_token": "tok-n4",
    })
    google.says("tok-n4", state=PURCHASED)

    assert _push(client, _purchased("tok-n4")).json()["status"] == "granted"
    assert len(_grants()) == 1


def test_a_purchase_naming_no_account_is_acknowledged_and_left_for_the_app(client, google):
    google.says("tok-n5", account=None)
    response = _push(client, _purchased("tok-n5"))

    assert response.status_code == 200
    assert response.json()["status"].startswith("ignored")
    assert _purchase("google", "tok-n5") is None


def test_google_unreachable_asks_pubsub_to_try_again(client, google):
    google.says("tok-n6")
    google.unreachable = True
    assert _push(client, _purchased("tok-n6")).status_code == 503


def test_a_voided_purchase_takes_the_days_back_once(client, google):
    google.says("tok-n7")
    _push(client, _purchased("tok-n7"))

    assert _push(client, _voided("tok-n7")).json()["status"] == "refunded"
    assert _push(client, _voided("tok-n7")).json()["status"].startswith("ignored")
    assert [g["days"] for g in _grants()] == [30, -30]


def test_another_apps_notification_and_googles_test_change_nothing(client, google):
    other = _purchased("tok-n8") | {"packageName": "com.someone.else"}
    assert _push(client, other).json()["status"].startswith("ignored")
    test = {"packageName": FakeGooglePlay.package_name, "testNotification": {"version": "1.0"}}
    assert _push(client, test).json()["status"] == "test"


def test_an_unreadable_push_is_acknowledged_rather_than_redelivered_for_ever(client, google):
    response = client.post("/billing/google/notifications", headers=PUSH,
                           json={"message": {"data": "%%%not base64%%%"}})
    assert response.status_code == 200
    assert response.json()["status"].startswith("ignored")


# --- Apple -------------------------------------------------------------------------------


def _apple(client, signed: str):
    return client.post("/billing/apple/notifications", json={"signedPayload": signed})


def test_an_apple_notification_that_does_not_verify_is_refused(client, apple_notes):
    assert _apple(client, "forged").status_code == 400
    assert client.post("/billing/apple/notifications", json={}).status_code == 400


def test_apple_charge_refund_and_reversal_each_land_once(client, apple_notes):
    apple_notes.says("charge", "ONE_TIME_CHARGE", "3000000001")
    apple_notes.says("refund", "REFUND", "3000000001")
    apple_notes.says("reversed", "REFUND_REVERSED", "3000000001")

    assert _apple(client, "charge").json()["status"] == "granted"
    assert _apple(client, "charge").json()["status"] == "already_granted"
    assert _apple(client, "refund").json()["status"] == "refunded"
    assert _apple(client, "refund").json()["status"].startswith("ignored")
    assert _apple(client, "reversed").json()["status"] == "restored"

    assert [g["days"] for g in _grants()] == [30, -30, 30]


def test_apple_lands_a_purchase_on_the_account_it_names(client, apple_notes):
    apple_notes.says("charge-stranger", "ONE_TIME_CHARGE", "3000000002",
                     account=STRANGER_TOKEN)
    _apple(client, "charge-stranger")

    assert _grants() == []
    assert len(_grants(STRANGER)) == 1


def test_apples_test_notification_changes_nothing(client, apple_notes):
    apple_notes.says("test", "TEST")
    assert _apple(client, "test").json()["status"] == "test"


def test_apple_unreachable_asks_apple_to_try_again(client, apple_notes):
    apple_notes.says("charge-later", "ONE_TIME_CHARGE", "3000000003")
    apple_notes.unreachable = True
    assert _apple(client, "charge-later").status_code == 503


# --- the reconciler ----------------------------------------------------------------------

SECRET = "test-reconcile-secret"


@pytest.fixture
def billing_on(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "jeene_billing_enabled", True)
    monkeypatch.setattr(settings, "jeene_reconcile_secret", SECRET)


def _sweep(client):
    return client.post("/internal/billing/reconcile", headers={"X-Jeene-Reconcile": SECRET})


def _age(store_ref: str, seconds: int = 600):
    """Make a purchase look as old as a sweep needs it to be."""
    _sql("UPDATE store_purchases SET created_at = now() - make_interval(secs => $2) "
         "WHERE store_ref = $1", store_ref, seconds)


def test_the_sweep_needs_its_secret(client, google, billing_on):
    assert client.post("/internal/billing/reconcile").status_code == 403


def test_the_sweep_with_billing_off_says_so_rather_than_failing(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "jeene_billing_enabled", False)
    monkeypatch.setattr(settings, "jeene_reconcile_secret", SECRET)
    response = _sweep(client)
    assert response.status_code == 200
    assert response.json()["status"] == "disabled"


def test_the_sweep_consumes_what_the_request_could_not(client, google, billing_on):
    google.says("tok-r1")
    google.consume_fails = True
    client.post("/billing/purchases", json={
        "store": "google", "product_id": SKU_30, "purchase_token": "tok-r1"})
    _age("tok-r1")
    google.consume_fails = False

    report = _sweep(client).json()

    assert report["consumed"] == 1
    assert google.consumed == ["tok-r1"]
    assert _purchase("google", "tok-r1")["consumed_at"] is not None


def test_the_sweep_grants_a_pending_purchase_google_has_since_been_paid(client, google,
                                                                          billing_on):
    google.says("tok-r2", state=PENDING)
    client.post("/billing/purchases", json={
        "store": "google", "product_id": SKU_30, "purchase_token": "tok-r2"})
    _age("tok-r2")
    google.says("tok-r2", state=PURCHASED)

    report = _sweep(client).json()

    assert report["granted"] == 1
    assert len(_grants()) == 1
    assert google.consumed == ["tok-r2"]


def test_the_sweep_applies_a_refund_nobody_announced(client, google, billing_on):
    google.says("tok-r3")
    client.post("/billing/purchases", json={
        "store": "google", "product_id": SKU_30, "purchase_token": "tok-r3"})

    async def voided(since):
        return [VoidedPurchase(purchase_token="tok-r3", order_id="GPA.r3",
                               voided_at=datetime.now(timezone.utc))]
    google.voided_since = voided

    first = _sweep(client).json()
    second = _sweep(client).json()

    assert (first["refunded"], second["refunded"]) == (1, 0)
    assert [g["days"] for g in _grants()] == [30, -30]


def test_the_sweep_reads_apples_undelivered_notifications(client, google, apple_notes,
                                                          billing_on):
    apple_notes.says("missed-charge", "ONE_TIME_CHARGE", "3000000004")
    apple_notes.history = ["missed-charge"]

    report = _sweep(client).json()

    assert report["granted"] == 1
    assert len(_grants()) == 1
    assert _sweep(client).json()["granted"] == 0, "read twice, granted once"
