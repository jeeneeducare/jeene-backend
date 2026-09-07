"""The four ways a payment gets confirmed, and what each one refuses to believe.

The endpoints in front of [app.billing.settle]. The state machine underneath is proven in
`test_billing_settle.py`; what is asserted here is the checking that happens *before*
anything reaches it, because that is where a patched client would attack:

  * **verify** — a signature proves the callback is genuine; only a server-to-server
    fetch proves money moved, and nothing is granted without one;
  * **failed** — a client's word that checkout failed, which is ignored outright if the
    gateway says something was captured;
  * **webhook** — no user token at all, so the HMAC over the raw bytes is the entire
    authentication;
  * **reconcile** — a shared secret, and elapsed time alone never fails a payment.

Signature verification is done by the real `RazorpayGateway`, not by a stub that returns
True for the string "good". Only the HTTP calls are faked. A test suite that fakes the
signature check is a test suite that would pass with the check removed.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import db
from app.billing.gateway import GatewayError, GatewayPayment
from app.billing.razorpay_gateway import RazorpayGateway

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="confirming a payment writes to the database"
)

TENANT = "JEENE_MASTER"
STUDENT = "test-confirm-student"
STRANGER = "test-confirm-stranger"

KEY_ID = "rzp_test_fake"
KEY_SECRET = "fake-key-secret-for-tests"
WEBHOOK_SECRET = "fake-webhook-secret-for-tests"

#: The real signing code, used to mint the signatures the tests send. Both sides of every
#: signature assertion therefore run the production HMAC.
SIGNER = RazorpayGateway(
    key_id=KEY_ID, key_secret=KEY_SECRET, webhook_secret=WEBHOOK_SECRET
)


def _sql(statement, *args):
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await db._init_connection(conn)
        try:
            return await conn.fetch(statement, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


class StubbedGateway(RazorpayGateway):
    """Razorpay with the network removed and nothing else changed.

    Every signature check is the inherited, real one. Only the three calls that would
    reach the internet are answered from `attempts`, which each test sets to whatever
    Razorpay would be saying at that moment.
    """

    def __init__(self) -> None:
        super().__init__(key_id=KEY_ID, key_secret=KEY_SECRET,
                         webhook_secret=WEBHOOK_SECRET)
        self.attempts: list[GatewayPayment] = []
        self.unreachable = False
        self.calls: list[str] = []

    def says(self, status: str, payment_id: str = "pay_stub", order_id: str = "") -> None:
        self.attempts = [GatewayPayment(
            payment_id=payment_id, order_id=order_id, status=status, amount_paise=19900,
            raw={"id": payment_id, "status": status},
        )]

    async def create_order(self, **kwargs):  # pragma: no cover - not exercised here
        raise AssertionError("these tests do not create orders")

    async def fetch_payment(self, payment_id):
        self.calls.append(f"fetch_payment:{payment_id}")
        if self.unreachable:
            raise GatewayError("gateway down")
        for attempt in self.attempts:
            if attempt.payment_id == payment_id:
                return attempt
        return GatewayPayment(payment_id=payment_id, order_id="", status="created",
                              amount_paise=0)

    async def fetch_order_payments(self, order_id):
        self.calls.append(f"fetch_order_payments:{order_id}")
        if self.unreachable:
            raise GatewayError("gateway down")
        return list(self.attempts)


@pytest.fixture(scope="module", autouse=True)
def students():
    for uid in (STUDENT, STRANGER):
        _sql(
            """INSERT INTO users (firebase_uid, tenant_id) VALUES ($1, $2)
               ON CONFLICT (firebase_uid) DO NOTHING""",
            uid, TENANT,
        )
    yield
    _sql("DELETE FROM entitlement_grants WHERE firebase_uid = ANY($1)", [STUDENT, STRANGER])
    _sql("DELETE FROM payments WHERE firebase_uid = ANY($1)", [STUDENT, STRANGER])
    _sql("DELETE FROM users WHERE firebase_uid = ANY($1)", [STUDENT, STRANGER])


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
def gw():
    from app import billing

    stub = StubbedGateway()
    billing.set_gateway(stub)
    yield stub
    billing.set_gateway(None)


@pytest.fixture
def order():
    """A pending payment belonging to STUDENT, cleaned up afterwards."""
    product_id = f"test_prod_{uuid.uuid4().hex[:8]}"
    order_id = f"order_test_{uuid.uuid4().hex[:12]}"
    _sql(
        """INSERT INTO products (product_id, tenant_id, title, amount_paise, duration_days)
           VALUES ($1, $2, 'Test Pass', 19900, 30)""",
        product_id, TENANT,
    )
    _sql(
        """INSERT INTO payments (order_id, firebase_uid, tenant_id, product_id,
                                 amount_paise, currency, duration_days)
           VALUES ($1, $2, $3, $4, 19900, 'INR', 30)""",
        order_id, STUDENT, TENANT, product_id,
    )
    yield order_id
    _sql("DELETE FROM entitlement_grants WHERE order_id = $1", order_id)
    _sql("UPDATE users SET pro_expires_at = NULL WHERE firebase_uid = ANY($1)",
         [STUDENT, STRANGER])
    _sql("DELETE FROM payments WHERE product_id = $1", product_id)
    _sql("DELETE FROM products WHERE product_id = $1", product_id)


def _status(order_id: str) -> str:
    return _sql("SELECT status FROM payments WHERE order_id = $1", order_id)[0]["status"]


def _grants(order_id: str) -> int:
    return len(_sql("SELECT 1 FROM entitlement_grants WHERE order_id = $1", order_id))


def _signed(order_id: str, payment_id: str) -> dict:
    from app.billing.gateway import hmac_sha256_hex

    return {
        "order_id": order_id,
        "payment_id": payment_id,
        "signature": hmac_sha256_hex(KEY_SECRET, f"{order_id}|{payment_id}".encode()),
    }


def _webhook(event: str, order_id: str, payment_id: str = "pay_hook", **extra) -> tuple:
    """A webhook body and the header that authenticates it, signed over the exact bytes."""
    from app.billing.gateway import hmac_sha256_hex

    entity = {"id": payment_id, "order_id": order_id, "status": "captured", **extra}
    body = json.dumps(
        {"event": event, "payload": {"payment": {"entity": entity}}}
    ).encode()
    return body, hmac_sha256_hex(WEBHOOK_SECRET, body)


# --- verify -----------------------------------------------------------------------------


def test_a_signed_and_captured_payment_is_paid_and_unlocks_immediately(client, gw, order):
    gw.says("captured", "pay_ok", order_id=order)

    body = client.post("/billing/verify", json=_signed(order, "pay_ok")).json()

    assert body["status"] == "paid"
    assert body["entitlement"]["active"] is True
    assert body["entitlement"]["days_remaining"] >= 29
    assert _status(order) == "paid"
    assert _grants(order) == 1


def test_a_signature_alone_does_not_pay_for_anything(client, gw, order):
    """The assertion the whole verify path exists for.

    An authorised-but-uncaptured payment signs *identically* to a captured one. If the
    signature were the end of it, every abandoned checkout would grant a year of Pro.
    """
    gw.says("authorized", "pay_held", order_id=order)

    body = client.post("/billing/verify", json=_signed(order, "pay_held")).json()

    assert body["status"] == "pending", "not failed either — it may still be in flight"
    assert body["entitlement"] is None
    assert _grants(order) == 0
    assert "fetch_payment:pay_held" in gw.calls, "the gateway is asked, always"


def test_a_forged_success_callback_is_refused(client, gw, order):
    response = client.post("/billing/verify", json={
        "order_id": order, "payment_id": "pay_forged", "signature": "0" * 64,
    })

    assert response.status_code == 400
    assert _status(order) == "pending"
    assert gw.calls == [], "a bad signature never reaches the gateway"


def test_somebody_elses_order_is_not_found(client, gw, order):
    _sql("UPDATE payments SET firebase_uid = $2 WHERE order_id = $1", order, STRANGER)

    response = client.post("/billing/verify", json=_signed(order, "pay_ok"))

    assert response.status_code == 404
    assert gw.calls == []


def test_a_payment_for_a_different_order_is_refused(client, gw, order):
    """Belt and braces: the signature covers the pairing, so this should be unreachable."""
    gw.says("captured", "pay_elsewhere", order_id="order_someone_else")

    response = client.post("/billing/verify", json=_signed(order, "pay_elsewhere"))

    assert response.status_code == 400
    assert _status(order) == "pending"
    assert _grants(order) == 0


def test_an_unreachable_gateway_leaves_the_payment_pending(client, gw, order):
    """Nothing is settled on a guess. The reconciler picks this up minutes later."""
    gw.unreachable = True

    response = client.post("/billing/verify", json=_signed(order, "pay_ok"))

    assert response.status_code == 502
    assert _status(order) == "pending"


def test_verifying_twice_grants_access_once(client, gw, order):
    gw.says("captured", "pay_ok", order_id=order)
    payload = _signed(order, "pay_ok")

    first = client.post("/billing/verify", json=payload).json()
    second = client.post("/billing/verify", json=payload).json()

    assert first["status"] == second["status"] == "paid"
    assert second["entitlement"]["active"] is True
    assert _grants(order) == 1


# --- the app reporting a failure ----------------------------------------------------------


def test_a_cancelled_checkout_releases_the_payment_straight_away(client, gw, order):
    gw.attempts = []

    body = client.post(
        "/billing/failed", json={"order_id": order, "reason": "cancelled by the user"}
    ).json()

    assert body["status"] == "failed"
    assert _status(order) == "failed"
    assert _grants(order) == 0
    row = _sql("SELECT failure_reason FROM payments WHERE order_id = $1", order)[0]
    assert row["failure_reason"] == "cancelled by the user"


def test_a_client_cannot_mark_its_own_captured_payment_as_failed(client, gw, order):
    """The attack: pay, claim it failed, keep the access and ask for the money back."""
    gw.says("captured", "pay_ok", order_id=order)

    body = client.post(
        "/billing/failed", json={"order_id": order, "reason": "it definitely failed"}
    ).json()

    assert body["status"] == "paid", "the gateway is asked before the claim is acted on"
    assert body["entitlement"]["active"] is True
    assert _status(order) == "paid"
    assert _grants(order) == 1


def test_a_payment_still_in_flight_is_not_failed_on_the_client_saying_so(client, gw, order):
    """A UPI intent handed to another app looks abandoned from here, until it is not."""
    gw.says("created", "pay_maybe", order_id=order)

    body = client.post(
        "/billing/failed", json={"order_id": order, "reason": "closed the sheet"}
    ).json()

    assert body["status"] == "pending"
    assert _status(order) == "pending"


def test_the_reason_a_client_sends_is_flattened_before_it_is_stored(client, gw, order):
    """The one field a client writes into this table, and a person reads back."""
    gw.attempts = []
    noisy = "line one\nline two\r\n\x07" + "x" * 400

    client.post("/billing/failed", json={"order_id": order, "reason": noisy})

    stored = _sql("SELECT failure_reason FROM payments WHERE order_id = $1", order)[0]
    reason = stored["failure_reason"]
    assert "\n" not in reason and "\x07" not in reason
    assert reason.startswith("line one line two x")
    assert len(reason) <= 200


def test_a_failure_report_for_somebody_elses_order_is_not_found(client, gw, order):
    _sql("UPDATE payments SET firebase_uid = $2 WHERE order_id = $1", order, STRANGER)

    response = client.post("/billing/failed", json={"order_id": order, "reason": ""})

    assert response.status_code == 404
    assert _status(order) == "pending"


def test_a_failure_report_is_not_acted_on_when_the_gateway_is_unreachable(client, gw, order):
    gw.unreachable = True

    response = client.post("/billing/failed", json={"order_id": order, "reason": "x"})

    assert response.status_code == 502
    assert _status(order) == "pending"


# --- the webhook --------------------------------------------------------------------------


def test_the_webhook_takes_no_user_token(client):
    """Razorpay has none to give. The HMAC is the whole of the authentication."""
    from app.auth import optional_user, require_user
    from app.routers import billing as billing_router

    route = next(r for r in billing_router.router.routes if r.path == "/billing/webhook")
    depends = {d.call for d in route.dependant.dependencies}
    assert require_user not in depends
    assert optional_user not in depends


def test_an_unsigned_webhook_changes_nothing(client, gw, order):
    body, _ = _webhook("payment.captured", order)

    response = client.post("/billing/webhook", content=body)

    assert response.status_code == 400
    assert _status(order) == "pending"


def test_a_webhook_signed_over_different_bytes_is_refused(client, gw, order):
    """The signature covers what was sent, not what it meant.

    Same JSON, different bytes: re-ordered keys, different spacing. Verifying against a
    re-serialised body instead of the raw one would let this through, and letting it
    through means accepting a body an attacker chose.
    """
    body, signature = _webhook("payment.captured", order)
    rearranged = json.dumps(json.loads(body), sort_keys=True, indent=1).encode()
    assert rearranged != body

    response = client.post(
        "/billing/webhook", content=rearranged,
        headers={"X-Razorpay-Signature": signature},
    )

    assert response.status_code == 400
    assert _status(order) == "pending"


def test_a_captured_webhook_pays_and_grants(client, gw, order):
    body, signature = _webhook("payment.captured", order, payment_id="pay_hook")

    response = client.post(
        "/billing/webhook", content=body, headers={"X-Razorpay-Signature": signature}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "paid"
    assert _status(order) == "paid"
    assert _grants(order) == 1
    stored = _sql("SELECT gateway_payload FROM payments WHERE order_id = $1", order)[0]
    assert stored["gateway_payload"]["id"] == "pay_hook", "stored as jsonb, not as a string"


def test_an_order_paid_webhook_settles_and_keeps_the_payment_id(client, gw, order):
    """It carries both entities. The payment id is the one a refund is issued against."""
    from app.billing.gateway import hmac_sha256_hex

    body = json.dumps({
        "event": "order.paid",
        "payload": {
            "payment": {"entity": {"id": "pay_from_order", "order_id": order,
                                   "status": "captured"}},
            "order": {"entity": {"id": order, "status": "paid"}},
        },
    }).encode()
    signature = hmac_sha256_hex(WEBHOOK_SECRET, body)

    response = client.post(
        "/billing/webhook", content=body, headers={"X-Razorpay-Signature": signature}
    )

    assert response.json()["status"] == "paid"
    row = _sql("SELECT razorpay_payment_id FROM payments WHERE order_id = $1", order)[0]
    assert row["razorpay_payment_id"] == "pay_from_order"
    assert _grants(order) == 1


def test_the_same_webhook_five_times_is_five_times_fine(client, gw, order):
    """Razorpay retries. A non-200 asks it to retry again, so duplicates answer 200."""
    body, signature = _webhook("payment.captured", order)

    codes = [
        client.post("/billing/webhook", content=body,
                    headers={"X-Razorpay-Signature": signature}).status_code
        for _ in range(5)
    ]

    assert codes == [200] * 5
    assert _grants(order) == 1


def test_a_failed_attempt_is_recorded_without_ending_the_payment(client, gw, order):
    """One attempt failing is not the order failing. The student can still pay."""
    body, signature = _webhook(
        "payment.failed", order, error_description="the card was declined"
    )

    response = client.post(
        "/billing/webhook", content=body, headers={"X-Razorpay-Signature": signature}
    )

    assert response.json()["status"] == "noted"
    assert _status(order) == "pending", "still open, because a retry is the normal case"
    row = _sql("SELECT failure_reason FROM payments WHERE order_id = $1", order)[0]
    assert row["failure_reason"] == "the card was declined", "kept for support"
    assert _grants(order) == 0


def test_a_mistyped_card_and_then_a_successful_retry_is_an_ordinary_purchase(
    client, gw, order
):
    """The sequence this rule exists for, and the one that would otherwise break.

    Razorpay sends `payment.failed` for every attempt, so a declined first try followed by
    a successful second is two webhooks against one order. Ending the payment on the first
    would route the capture into manual review — for a student who did nothing wrong and
    is looking at a success screen.
    """
    failed, failed_sig = _webhook(
        "payment.failed", order, payment_id="pay_try1", error_description="declined"
    )
    captured, captured_sig = _webhook(
        "payment.captured", order, payment_id="pay_try2"
    )

    client.post("/billing/webhook", content=failed,
                headers={"X-Razorpay-Signature": failed_sig})
    second = client.post("/billing/webhook", content=captured,
                         headers={"X-Razorpay-Signature": captured_sig})

    assert second.json()["status"] == "paid"
    assert _status(order) == "paid"
    assert _grants(order) == 1


def test_a_webhook_for_an_order_we_never_created_is_acknowledged(client, gw):
    """Almost always a webhook aimed at the wrong environment. 200, or it retries forever."""
    body, signature = _webhook("payment.captured", "order_from_another_world")

    response = client.post(
        "/billing/webhook", content=body, headers={"X-Razorpay-Signature": signature}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "unknown"


def test_an_event_we_do_not_act_on_is_acknowledged(client, gw, order):
    body, signature = _webhook("payment.authorized", order)

    response = client.post(
        "/billing/webhook", content=body, headers={"X-Razorpay-Signature": signature}
    )

    assert response.json()["status"] == "ignored"
    assert _status(order) == "pending"


# --- the race the whole design is for ------------------------------------------------------


def test_the_webhook_and_the_app_confirming_together_grant_access_once(client, gw, order):
    gw.says("captured", "pay_hook", order_id=order)
    body, signature = _webhook("payment.captured", order, payment_id="pay_hook")

    from_app = client.post("/billing/verify", json=_signed(order, "pay_hook")).json()
    from_razorpay = client.post(
        "/billing/webhook", content=body, headers={"X-Razorpay-Signature": signature}
    ).json()

    assert from_app["status"] == from_razorpay["status"] == "paid"
    assert _grants(order) == 1


def test_the_same_race_in_the_other_order_grants_access_once(client, gw, order):
    gw.says("captured", "pay_hook", order_id=order)
    body, signature = _webhook("payment.captured", order, payment_id="pay_hook")

    client.post("/billing/webhook", content=body,
                headers={"X-Razorpay-Signature": signature})
    from_app = client.post("/billing/verify", json=_signed(order, "pay_hook")).json()

    assert from_app["status"] == "paid"
    assert from_app["entitlement"]["active"] is True, "the app still gets its answer"
    assert _grants(order) == 1


def test_a_late_capture_after_the_reconciler_gave_up_goes_to_a_person(client, gw, order):
    """Both halves of the worst race, through the endpoints rather than the mutator."""
    client.post("/billing/failed", json={"order_id": order, "reason": "cancelled"})
    assert _status(order) == "failed"

    body, signature = _webhook("payment.captured", order, payment_id="pay_late")
    response = client.post(
        "/billing/webhook", content=body, headers={"X-Razorpay-Signature": signature}
    )

    assert response.status_code == 200
    assert response.json()["status"] == "needs_manual_review"
    assert _status(order) == "needs_manual_review"
    assert _grants(order) == 0, "a person decides whether to grant or refund"
