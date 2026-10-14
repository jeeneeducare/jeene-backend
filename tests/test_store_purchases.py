"""A store purchase, handed over by the app: what is believed, and what is granted.

The route in front of [app.billing.purchases], with both stores faked at the network
boundary. The fakes answer only what Google and Apple would answer; every decision —
who owns the purchase, whether it grants, whether it grants twice — is the real code
against a real database:

  * a paid purchase grants its pass once, however many times it is handed over;
  * a pending one grants nothing and fails nothing, until the store says it is paid;
  * a purchase lands on the account it names, not on whoever is signed in;
  * a refund takes the days back once, and Apple reversing the refund gives them back;
  * nothing the app says about a purchase is used — only the store's answer.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import db
from app.billing.stores import (
    APPLE,
    CANCELLED,
    GOOGLE,
    PENDING,
    PRODUCTION,
    PURCHASED,
    REVOKED,
    SANDBOX,
    AppleNotification,
    ProofRejected,
    StorePurchase,
    StoreUnreachable,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="recording a purchase writes to the database"
)

TENANT = "JEENE_MASTER"
STUDENT = "test-store-student"
STRANGER = "test-store-stranger"
STUDENT_TOKEN = "6b0d6a3e-2f0c-4a53-9d3e-1f2a3b4c5d6e"
STRANGER_TOKEN = "0f1e2d3c-4b5a-4968-8776-655443322110"

PASS_30 = "test_store_pass_30"
PASS_90 = "test_store_pass_90"
RETIRED = "test_store_pass_old"
SKU_30 = "test.store.pass.30d"
SKU_90 = "test.store.pass.90d"
SKU_OLD = "test.store.pass.old"


def _run(coroutine_factory):
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await db._init_connection(conn)
        try:
            return await coroutine_factory(conn)
        finally:
            await conn.close()
    return asyncio.run(go())


def _sql(statement, *args):
    return _run(lambda conn: conn.fetch(statement, *args))


class FakeGooglePlay:
    """Google Play with the network removed. Answers whatever a test says Google would."""

    package_name = "com.jeene.test"

    def __init__(self) -> None:
        self.records: dict[str, StorePurchase] = {}
        self.consumed: list[str] = []
        self.unreachable = False
        self.consume_fails = False

    def says(self, token: str, *, sku: str = SKU_30, state: str = PURCHASED,
             account: str | None = STUDENT_TOKEN, test: bool = False) -> None:
        self.records[token] = StorePurchase(
            store=GOOGLE, ref=token, store_product_id=sku, state=state,
            environment=SANDBOX if test else PRODUCTION, account_token=account,
            order_id=f"GPA.{token}", purchased_at=datetime.now(timezone.utc),
            raw={"token": token, "state": state},
        )

    async def get_purchase(self, purchase_token: str) -> StorePurchase:
        if self.unreachable:
            raise StoreUnreachable("down")
        if purchase_token not in self.records:
            raise ProofRejected("Google has never issued this token")
        record = self.records[purchase_token]
        if purchase_token in self.consumed:
            record = StorePurchase(**{**record.__dict__, "consumed": True})
        return record

    async def consume(self, store_product_id: str, purchase_token: str) -> None:
        if self.consume_fails:
            raise StoreUnreachable("down")
        self.consumed.append(purchase_token)

    async def voided_since(self, since):
        return []

    async def verify_push(self, authorization: str) -> None:
        if authorization != "Bearer google-signed":
            raise ProofRejected("not Google")


class FakeAppStore:
    """The App Store with the certificate check removed. Signed blobs are looked up by name."""

    def __init__(self) -> None:
        self.transactions: dict[str, StorePurchase] = {}

    def signs(self, signed: str, transaction_id: str, *, sku: str = SKU_30,
              account: str | None = STUDENT_TOKEN, revoked: bool = False,
              sandbox: bool = False) -> None:
        self.transactions[signed] = StorePurchase(
            store=APPLE, ref=transaction_id, store_product_id=sku,
            state=REVOKED if revoked else PURCHASED,
            environment=SANDBOX if sandbox else PRODUCTION, account_token=account,
            order_id=transaction_id, purchased_at=datetime.now(timezone.utc),
            price_micros=199_000_000, currency="INR", raw={"transactionId": transaction_id},
        )

    async def verify_transaction(self, signed_transaction: str) -> StorePurchase:
        if signed_transaction not in self.transactions:
            raise ProofRejected("signature did not verify")
        return self.transactions[signed_transaction]

    async def verify_notification(self, signed_payload: str) -> AppleNotification:
        raise ProofRejected("not used here")

    async def notification_history(self, since):
        return []


signed_in = {"uid": STUDENT}


@pytest.fixture(scope="module", autouse=True)
def world():
    _sql("DELETE FROM entitlement_grants WHERE firebase_uid = ANY($1)", [STUDENT, STRANGER])
    _sql("DELETE FROM store_purchases WHERE firebase_uid = ANY($1)", [STUDENT, STRANGER])
    for uid, token in ((STUDENT, STUDENT_TOKEN), (STRANGER, STRANGER_TOKEN)):
        _sql(
            """INSERT INTO users (firebase_uid, tenant_id, store_account_token)
               VALUES ($1, $2, $3)
               ON CONFLICT (firebase_uid) DO UPDATE SET store_account_token = $3,
                                                        pro_expires_at = NULL""",
            uid, TENANT, uuid.UUID(token),
        )
    for product_id, sku, days, active in ((PASS_30, SKU_30, 30, True),
                                          (PASS_90, SKU_90, 90, True),
                                          (RETIRED, SKU_OLD, 30, False)):
        _sql(
            """INSERT INTO products (product_id, tenant_id, title, amount_paise, duration_days,
                                     active, google_product_id, apple_product_id)
               VALUES ($1, $2, 'Test pass', 19900, $3, $4, $5, $5)
               ON CONFLICT (product_id) DO NOTHING""",
            product_id, TENANT, days, active, sku,
        )
    yield
    _sql("DELETE FROM entitlement_grants WHERE firebase_uid = ANY($1)", [STUDENT, STRANGER])
    _sql("DELETE FROM store_purchases WHERE firebase_uid = ANY($1)", [STUDENT, STRANGER])
    _sql("DELETE FROM products WHERE product_id = ANY($1)", [PASS_30, PASS_90, RETIRED])
    _sql("DELETE FROM users WHERE firebase_uid = ANY($1)", [STUDENT, STRANGER])


@pytest.fixture(autouse=True)
def fresh_ledger():
    """Every test starts from a student who has never bought anything."""
    _sql("DELETE FROM entitlement_grants WHERE firebase_uid = ANY($1)", [STUDENT, STRANGER])
    _sql("DELETE FROM store_purchases WHERE firebase_uid = ANY($1)", [STUDENT, STRANGER])
    _sql("UPDATE users SET pro_expires_at = NULL WHERE firebase_uid = ANY($1)",
         [STUDENT, STRANGER])
    signed_in["uid"] = STUDENT
    from app.routers import billing as billing_router
    billing_router._recent_purchases.clear()
    yield


@pytest.fixture(scope="module")
def client():
    from app.auth import current_tenant, optional_user, require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: {"uid": signed_in["uid"]}
    app.dependency_overrides[optional_user] = lambda: {"uid": signed_in["uid"]}
    app.dependency_overrides[current_tenant] = lambda: TENANT
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def google():
    from app import billing

    fake = FakeGooglePlay()
    billing.set_google_play(fake)
    yield fake
    billing.set_google_play(None)


@pytest.fixture
def apple():
    from app import billing

    fake = FakeAppStore()
    billing.set_app_store(fake)
    yield fake
    billing.set_app_store(None)


def _hand_over(client, token: str, sku: str = SKU_30):
    return client.post("/billing/purchases", json={
        "store": "google", "product_id": sku, "purchase_token": token,
    })


def _grants(uid: str = STUDENT):
    return _sql(
        "SELECT days, provider, reverses_grant_id FROM entitlement_grants "
        "WHERE firebase_uid = $1 ORDER BY granted_at, grant_id", uid,
    )


def _purchase(store: str, ref: str):
    rows = _sql("SELECT * FROM store_purchases WHERE store = $1 AND store_ref = $2", store, ref)
    return rows[0] if rows else None


# --- Google ------------------------------------------------------------------------------


def test_a_paid_purchase_grants_its_pass_and_is_then_consumed(client, google):
    google.says("tok-paid")
    response = _hand_over(client, "tok-paid")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "granted"
    assert body["finish"] is True
    assert body["entitlement"]["active"] is True
    assert body["entitlement"]["days_remaining"] in (29, 30)

    row = _purchase("google", "tok-paid")
    assert row["status"] == "granted"
    assert row["product_id"] == PASS_30
    assert row["duration_days"] == 30
    assert row["consumed_at"] is not None, "consumed after the grant"
    assert google.consumed == ["tok-paid"]
    assert [(g["days"], g["provider"]) for g in _grants()] == [(30, "google")]


def test_handing_the_same_purchase_over_twice_grants_it_once(client, google):
    google.says("tok-twice")
    first = _hand_over(client, "tok-twice").json()
    second = _hand_over(client, "tok-twice").json()

    assert first["status"] == "granted"
    assert second["status"] == "already_granted"
    assert second["finish"] is True
    assert second["entitlement"]["expires_at"] == first["entitlement"]["expires_at"]
    assert len(_grants()) == 1


def test_two_passes_stack(client, google):
    google.says("tok-a")
    google.says("tok-b", sku=SKU_90)
    _hand_over(client, "tok-a")
    body = _hand_over(client, "tok-b", SKU_90).json()

    assert body["entitlement"]["days_remaining"] in (119, 120)


def test_a_pending_purchase_grants_nothing_until_it_is_paid(client, google):
    google.says("tok-upi", state=PENDING)
    waiting = _hand_over(client, "tok-upi").json()

    assert waiting["status"] == "pending"
    assert waiting["finish"] is False, "the phone keeps it until the store settles it"
    assert waiting["entitlement"]["active"] is False
    assert _grants() == []

    google.says("tok-upi", state=PURCHASED)
    paid = _hand_over(client, "tok-upi").json()
    assert paid["status"] == "granted"
    assert paid["entitlement"]["active"] is True
    assert len(_grants()) == 1


def test_a_pending_purchase_can_be_cancelled_and_then_grants_nothing(client, google):
    google.says("tok-gone", state=PENDING)
    _hand_over(client, "tok-gone")
    google.says("tok-gone", state=CANCELLED)
    body = _hand_over(client, "tok-gone").json()

    assert body["status"] == "cancelled"
    assert body["finish"] is True
    assert _grants() == []


def test_a_purchase_lands_on_the_account_it_names_not_on_whoever_hands_it_over(client, google):
    google.says("tok-sibling", account=STRANGER_TOKEN)
    body = _hand_over(client, "tok-sibling").json()

    assert body["status"] == "other_account"
    assert body["finish"] is True
    assert body["entitlement"]["active"] is False, "the student signed in now paid nothing"
    assert _grants() == []
    assert len(_grants(STRANGER)) == 1


def test_a_purchase_already_recorded_cannot_be_claimed_by_another_account(client, google):
    google.says("tok-mine", account=None)
    _hand_over(client, "tok-mine")

    signed_in["uid"] = STRANGER
    body = _hand_over(client, "tok-mine").json()

    assert body["status"] == "other_account"
    assert _grants(STRANGER) == []
    assert len(_grants(STUDENT)) == 1


def test_a_purchase_naming_an_account_we_never_issued_is_refused(client, google):
    google.says("tok-forged", account=str(uuid.uuid4()))
    response = _hand_over(client, "tok-forged")

    assert response.status_code == 409
    assert _purchase("google", "tok-forged") is None


def test_a_purchase_with_no_account_id_goes_to_the_student_who_handed_it_over(client, google):
    google.says("tok-anonymous", account=None)
    body = _hand_over(client, "tok-anonymous").json()

    assert body["status"] == "granted"
    assert len(_grants()) == 1


def test_a_token_google_never_issued_is_refused_and_nothing_is_written(client, google):
    response = _hand_over(client, "tok-made-up")

    assert response.status_code == 400
    assert _purchase("google", "tok-made-up") is None


def test_google_unreachable_is_a_retry_later_not_a_refusal(client, google):
    google.says("tok-later")
    google.unreachable = True
    response = _hand_over(client, "tok-later")

    assert response.status_code == 502
    assert "safe" in response.json()["detail"]
    assert _purchase("google", "tok-later") is None


def test_the_app_naming_another_product_changes_nothing(client, google):
    google.says("tok-liar", sku=SKU_30)
    body = _hand_over(client, "tok-liar", sku=SKU_90).json()

    assert body["entitlement"]["days_remaining"] in (29, 30), "Google said 30 days, so 30"


def test_a_store_product_that_is_no_pass_is_refused(client, google):
    google.says("tok-stranger-sku", sku="something.else")
    assert _hand_over(client, "tok-stranger-sku").status_code == 400


def test_a_retired_pass_bought_before_it_was_retired_still_lands(client, google):
    google.says("tok-old", sku=SKU_OLD)
    body = _hand_over(client, "tok-old", SKU_OLD).json()

    assert body["status"] == "granted"
    assert body["entitlement"]["active"] is True


def test_a_failed_consume_still_grants_and_leaves_it_for_the_reconciler(client, google):
    google.says("tok-unconsumed")
    google.consume_fails = True
    body = _hand_over(client, "tok-unconsumed").json()

    assert body["status"] == "granted"
    assert _purchase("google", "tok-unconsumed")["consumed_at"] is None


def test_a_test_purchase_grants_but_is_recorded_as_sandbox(client, google):
    google.says("tok-tester", test=True)
    body = _hand_over(client, "tok-tester").json()

    assert body["status"] == "granted"
    assert _purchase("google", "tok-tester")["environment"] == "sandbox"


def test_nothing_is_sold_while_billing_is_switched_off(client, monkeypatch):
    from app import billing
    from app.config import settings

    billing.set_google_play(None)
    monkeypatch.setattr(settings, "jeene_billing_enabled", False)
    response = _hand_over(client, "tok-off")

    assert response.status_code == 503


# --- refunds -----------------------------------------------------------------------------


def test_a_refund_takes_the_days_back_once(client, google):
    from app.billing import purchases

    google.says("tok-refund")
    _hand_over(client, "tok-refund")

    first = _run(lambda conn: purchases.void(conn, GOOGLE, "tok-refund"))
    second = _run(lambda conn: purchases.void(conn, GOOGLE, "tok-refund"))

    assert (first, second) == (True, False)
    assert [g["days"] for g in _grants()] == [30, -30]
    assert _purchase("google", "tok-refund")["status"] == "refunded"
    assert client.get("/billing/me").json()["active"] is False


def test_a_refunded_purchase_handed_over_again_is_not_granted_again(client, google):
    from app.billing import purchases

    google.says("tok-stale")
    _hand_over(client, "tok-stale")
    _run(lambda conn: purchases.void(conn, GOOGLE, "tok-stale"))

    body = _hand_over(client, "tok-stale").json()

    assert body["status"] == "refunded"
    assert body["entitlement"]["active"] is False
    assert [g["days"] for g in _grants()] == [30, -30]


def test_a_purchase_first_seen_revoked_grants_nothing(client, apple):
    apple.signs("signed-revoked", "2000000001", revoked=True)
    body = client.post("/billing/purchases", json={
        "store": "apple", "product_id": SKU_30, "signed_transaction": "signed-revoked",
    }).json()

    assert body["status"] == "refunded"
    assert body["finish"] is True
    assert _grants() == []


def test_a_refund_apple_reverses_gives_the_days_back_once(client, apple):
    from app.billing import purchases

    apple.signs("signed-1", "2000000002")
    client.post("/billing/purchases", json={
        "store": "apple", "product_id": SKU_30, "signed_transaction": "signed-1",
    })
    _run(lambda conn: purchases.void(conn, APPLE, "2000000002"))
    first = _run(lambda conn: purchases.restore(conn, APPLE, "2000000002"))
    second = _run(lambda conn: purchases.restore(conn, APPLE, "2000000002"))

    assert (first, second) == (True, False)
    assert [g["days"] for g in _grants()] == [30, -30, 30]
    assert _purchase("apple", "2000000002")["status"] == "granted"
    assert client.get("/billing/me").json()["active"] is True

    # And a second refund takes them back again: the chain keeps going.
    assert _run(lambda conn: purchases.void(conn, APPLE, "2000000002")) is True
    assert [g["days"] for g in _grants()] == [30, -30, 30, -30]


# --- Apple -------------------------------------------------------------------------------


def test_an_apple_purchase_is_granted_with_its_price_recorded(client, apple):
    apple.signs("signed-ok", "2000000003")
    body = client.post("/billing/purchases", json={
        "store": "apple", "product_id": SKU_30, "signed_transaction": "signed-ok",
    }).json()

    assert body["status"] == "granted"
    assert body["finish"] is True
    row = _purchase("apple", "2000000003")
    assert row["price_micros"] == 199_000_000
    assert row["currency"] == "INR"
    assert row["consumed_at"] is None, "nothing to consume at Apple"


def test_a_signature_apple_did_not_make_is_refused(client, apple):
    response = client.post("/billing/purchases", json={
        "store": "apple", "product_id": SKU_30, "signed_transaction": "forged",
    })
    assert response.status_code == 400


def test_a_purchase_without_its_proof_is_refused(client, google, apple):
    assert client.post("/billing/purchases", json={
        "store": "google", "product_id": SKU_30}).status_code == 400
    assert client.post("/billing/purchases", json={
        "store": "apple", "product_id": SKU_30}).status_code == 400


# --- the account id ----------------------------------------------------------------------


def test_the_account_id_is_made_once_and_then_kept(client):
    _sql("UPDATE users SET store_account_token = NULL WHERE firebase_uid = $1", STRANGER)
    signed_in["uid"] = STRANGER

    first = client.get("/billing/me").json()["account_token"]
    second = client.get("/billing/me").json()["account_token"]

    assert first and first == second
    assert str(uuid.UUID(first)) == first
    _sql("UPDATE users SET store_account_token = $2 WHERE firebase_uid = $1",
         STRANGER, uuid.UUID(STRANGER_TOKEN))
