"""Quoting a price, and turning it into an order.

This is where a patched client would try to pay ₹1 for a year, so most of what is
asserted here is a refusal. The single rule underneath all of it: **the app never sends
an amount.** It names a product; the server prices it, signs the price, and prices it
again before charging.

The gateway is a fake. Every decision worth testing — what to charge, when to refuse,
what order to do things in — is ours, and none of it needs the network to exercise.
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.billing import quotes
from app.billing.gateway import GatewayError, GatewayOrder, GatewayPayment

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="ordering reads and writes the database"
)

TENANT = "JEENE_MASTER"
STUDENT = "student-fresh"


class FakeGateway:
    """A gateway that records what it was asked and answers instantly.

    `fail_with` makes the next create_order raise, which is how the "external call first"
    ordering is proven: if it were the other way round, a pending row would survive a
    gateway that never accepted the order.
    """

    def __init__(self) -> None:
        self.orders: list[dict] = []
        self.fail_with: Exception | None = None

    async def create_order(self, *, amount_paise, currency, receipt, notes):
        if self.fail_with:
            raise self.fail_with
        self.orders.append(
            {"amount_paise": amount_paise, "currency": currency,
             "receipt": receipt, "notes": notes}
        )
        return GatewayOrder(
            order_id=f"order_fake_{uuid.uuid4().hex[:10]}",
            amount_paise=amount_paise,
            currency=currency,
        )

    async def fetch_payment(self, payment_id):
        return GatewayPayment(payment_id=payment_id, order_id="", status="captured",
                              amount_paise=0)

    async def fetch_order_payments(self, order_id):
        return []

    def verify_payment_signature(self, *, order_id, payment_id, signature):
        return signature == "good"

    def verify_webhook_signature(self, *, body, signature):
        return signature == "good"


def _sql(statement, *args):
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetch(statement, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


@pytest.fixture(scope="module")
def client():
    from app.auth import current_tenant, optional_user, require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: {"uid": STUDENT}
    app.dependency_overrides[optional_user] = lambda: {"uid": STUDENT}
    app.dependency_overrides[current_tenant] = lambda: TENANT
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def fake():
    """Install a fake gateway for one test, and clear the rate limiter with it."""
    from app import billing
    from app.routers import billing as billing_router

    gw = FakeGateway()
    billing.set_gateway(gw)
    billing_router._recent_orders.clear()
    yield gw
    billing.set_gateway(None)


@pytest.fixture
def product():
    pid = f"test_prod_{uuid.uuid4().hex[:8]}"
    _sql(
        """INSERT INTO products (product_id, tenant_id, title, amount_paise, duration_days)
           VALUES ($1, $2, 'Test Monthly', 19900, 30)""",
        pid, TENANT,
    )
    yield pid
    _sql("DELETE FROM payments WHERE product_id = $1", pid)
    _sql("DELETE FROM products WHERE product_id = $1", pid)


# --- quoting ---------------------------------------------------------------------------


def test_a_quote_prices_the_product_the_server_knows_about(client, product):
    body = client.post("/billing/quote", json={"product_id": product}).json()
    assert body["amount_paise"] == 19900
    assert body["duration_days"] == 30
    assert body["quote"]


def test_a_product_that_is_not_for_sale_cannot_be_quoted(client):
    response = client.post("/billing/quote", json={"product_id": "no_such_product"})
    assert response.status_code == 404


def test_a_retired_product_cannot_be_quoted(client, product):
    _sql("UPDATE products SET active = FALSE WHERE product_id = $1", product)
    assert client.post("/billing/quote", json={"product_id": product}).status_code == 404


# --- what a patched client would try ----------------------------------------------------


def test_an_edited_quote_is_refused(client, fake, product):
    """The whole point of the signature. Change one byte of the payload and it dies."""
    token = client.post("/billing/quote", json={"product_id": product}).json()["quote"]
    payload, _, digest = token.partition(".")
    tampered = f"{payload[:-2]}XY.{digest}"

    response = client.post("/billing/orders", json={"quote": tampered})
    assert response.status_code == 400
    assert fake.orders == [], "a bad quote must never reach the gateway"


def test_a_quote_minted_for_somebody_else_is_refused(client, fake, product):
    """Lifting a token off another device buys nothing: it is bound to a uid."""
    someone_else = quotes.sign(
        uid="a-different-student", product_id=product, amount_paise=1,
        currency="INR", duration_days=365,
    )
    response = client.post("/billing/orders", json={"quote": someone_else})
    assert response.status_code == 400
    assert fake.orders == []


def test_an_expired_quote_is_refused(client, fake, product):
    stale = quotes.sign(
        uid=STUDENT, product_id=product, amount_paise=19900, currency="INR",
        duration_days=30, ttl_seconds=-1,
    )
    response = client.post("/billing/orders", json={"quote": stale})
    assert response.status_code == 400
    assert fake.orders == []


def test_a_forged_quote_signed_with_the_wrong_secret_is_refused(client, fake, product):
    """A token shaped exactly right, signed by somebody who does not have the secret."""
    import base64
    import hashlib
    import hmac
    import json

    payload = json.dumps({
        "uid": STUDENT, "product_id": product, "amount_paise": 100, "currency": "INR",
        "duration_days": 365, "iat": int(time.time()), "exp": int(time.time()) + 600,
        "nonce": "x",
    }, separators=(",", ":"), sort_keys=True).encode()
    forged = (
        base64.urlsafe_b64encode(payload).decode().rstrip("=")
        + "." + hmac.new(b"not-the-secret", payload, hashlib.sha256).hexdigest()
    )

    response = client.post("/billing/orders", json={"quote": forged})
    assert response.status_code == 400
    assert fake.orders == []


def test_the_price_is_taken_from_the_database_and_not_from_the_quote(client, fake, product):
    """A quote saying ₹1 does not make the charge ₹1 — the mismatch is caught.

    This is the assertion that matters most on this page. Even a *validly signed* quote
    is re-checked against the product, so a leaked signing secret alone still cannot set
    a price.
    """
    cheap = quotes.sign(
        uid=STUDENT, product_id=product, amount_paise=100, currency="INR", duration_days=30
    )
    response = client.post("/billing/orders", json={"quote": cheap})

    assert response.status_code == 409
    assert fake.orders == [], "nothing reaches the gateway when the price disagrees"


def test_a_price_change_inside_the_ttl_refuses_rather_than_guessing(client, fake, product):
    """Neither figure is right: the new one was never agreed, the old one is not the price."""
    token = client.post("/billing/quote", json={"product_id": product}).json()["quote"]
    _sql("UPDATE products SET amount_paise = 24900 WHERE product_id = $1", product)

    response = client.post("/billing/orders", json={"quote": token})
    assert response.status_code == 409
    assert fake.orders == []


# --- the happy path, and the order things happen in --------------------------------------


def test_a_good_quote_becomes_an_order_at_the_price_on_the_product(client, fake, product):
    token = client.post("/billing/quote", json={"product_id": product}).json()["quote"]
    body = client.post("/billing/orders", json={"quote": token}).json()

    assert body["amount_paise"] == 19900
    assert body["order_id"].startswith("order_fake_")
    assert fake.orders[0]["amount_paise"] == 19900, "the gateway is asked for the real price"


def test_the_order_row_copies_the_price_rather_than_pointing_at_it(client, fake, product):
    """So repricing tomorrow cannot rewrite what somebody paid today."""
    token = client.post("/billing/quote", json={"product_id": product}).json()["quote"]
    order_id = client.post("/billing/orders", json={"quote": token}).json()["order_id"]

    _sql("UPDATE products SET amount_paise = 99900 WHERE product_id = $1", product)
    row = _sql("SELECT amount_paise, duration_days, status FROM payments WHERE order_id = $1",
               order_id)[0]

    assert row["amount_paise"] == 19900, "the payment remembers what was charged"
    assert row["duration_days"] == 30
    assert row["status"] == "pending"


def test_the_gateway_is_called_before_the_row_is_written(client, fake, product):
    """A crash between the two must not leave a pending row with no order behind it.

    Proven by making the gateway fail: if the row were written first, one would survive.
    """
    token = client.post("/billing/quote", json={"product_id": product}).json()["quote"]
    fake.fail_with = GatewayError("gateway down")

    response = client.post("/billing/orders", json={"quote": token})
    assert response.status_code == 502
    assert _sql("SELECT order_id FROM payments WHERE product_id = $1", product) == []


def test_the_student_is_carried_in_the_notes_for_the_webhook_to_find(client, fake, product):
    """So a webhook can be traced to a person without trusting anything the client said."""
    token = client.post("/billing/quote", json={"product_id": product}).json()["quote"]
    client.post("/billing/orders", json={"quote": token})

    assert fake.orders[0]["notes"]["firebase_uid"] == STUDENT
    assert fake.orders[0]["notes"]["product_id"] == product


def test_only_the_publishable_key_reaches_the_app(client, fake, product):
    from app.config import settings

    token = client.post("/billing/quote", json={"product_id": product}).json()["quote"]
    body = client.post("/billing/orders", json={"quote": token}).json()

    assert set(body) == {"order_id", "key_id", "amount_paise", "currency",
                         "product_id", "title"}
    if settings.razorpay_key_secret:
        assert settings.razorpay_key_secret not in str(body)


# --- refusing to take money badly --------------------------------------------------------


def test_orders_are_refused_when_billing_is_not_configured(client, product):
    """A half-configured deployment must not take a payment it cannot confirm."""
    from app import billing
    from app.routers import billing as billing_router

    token = client.post("/billing/quote", json={"product_id": product}).json()["quote"]
    billing.set_gateway(None)
    billing_router._recent_orders.clear()

    response = client.post("/billing/orders", json={"quote": token})
    assert response.status_code == 503


def test_a_flood_of_orders_is_capped(client, fake, product):
    from app.routers import billing as billing_router

    token = client.post("/billing/quote", json={"product_id": product}).json()["quote"]
    codes = [client.post("/billing/orders", json={"quote": token}).status_code
             for _ in range(billing_router._ORDERS_PER_HOUR + 2)]
    assert 429 in codes


def test_a_student_with_no_profile_never_reaches_the_gateway(client, fake, product):
    """The order would be real and the local row would not.

    `payments.firebase_uid` references `users`, so a student whose profile has never been
    synced used to create an order at Razorpay and *then* die on the foreign key. What is
    left behind is an order nobody has a record of — the one shape the reconciler cannot
    see, because it sweeps the payments table.
    """
    from app.auth import require_user
    from app.main import app

    token = client.post("/billing/quote", json={"product_id": product}).json()["quote"]
    stranger = "student-who-never-signed-in"
    app.dependency_overrides[require_user] = lambda: {"uid": stranger}
    try:
        # A quote for the stranger, since the one above is bound to STUDENT.
        theirs = quotes.sign(
            uid=stranger, product_id=product, amount_paise=19900, currency="INR",
            duration_days=30,
        )
        response = client.post("/billing/orders", json={"quote": theirs})
    finally:
        app.dependency_overrides[require_user] = lambda: {"uid": STUDENT}

    assert response.status_code == 409
    assert fake.orders == [], "nothing was created at Razorpay"
    assert token  # the fixture's quote is unused here; kept so the product exists


def test_every_order_carries_its_own_receipt(client, fake, product):
    """Razorpay treats the receipt as the merchant's reference for *one* order.

    Keyed on the student alone it repeated for every purchase they ever made, which is
    useless for reconciling a statement and fails outright on an account with unique
    receipts enforced.
    """
    for _ in range(2):
        token = client.post("/billing/quote", json={"product_id": product}).json()["quote"]
        client.post("/billing/orders", json={"quote": token})

    receipts = [o["receipt"] for o in fake.orders]
    assert len(receipts) == 2
    assert receipts[0] != receipts[1]
    assert all(len(r) <= 40 for r in receipts), "Razorpay caps a receipt at forty"


def test_a_quote_with_odd_characters_is_refused_rather_than_fatal(client, fake, product):
    """A quote is a client-supplied string, and `compare_digest` raises on non-ASCII.

    Left alone this answered 500 rather than "that price is no longer valid" — on the one
    route that creates a real order at a payment gateway.
    """
    for quote in ("payload.café", "é.é", "🙂.🙂"):
        response = client.post("/billing/orders", json={"quote": quote})
        assert response.status_code == 400, f"{quote!r} -> {response.status_code}"

    assert fake.orders == []
