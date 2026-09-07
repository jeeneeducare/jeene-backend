"""The sweep that closes the purchases nothing else closed.

The other three confirmation paths all depend on a message arriving. This one depends on
nothing: it asks Razorpay directly about every payment still sitting at `pending`. It is
the reason a student whose phone died on the success screen still gets what they paid for.

Two rules are what these tests are really about, and both are about *not* acting:

  * **elapsed time never fails a payment.** The gateway is asked first, every time. A
    student on a slow connection still typing an OTP has no attempts against their order,
    and failing them mid-payment would be a bug that only ever bites the least connected.
  * **held money goes to a person.** An authorisation that never captured is money the
    student has parted with. Telling them it failed, while their bank shows a hold, is
    the one answer nobody can act on.

The sweep is scoped by asserting on the rows it was pointed at: any other pending payment
in the test database is aged out of the window first, so each test sees its own row only.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import db
from app.billing.gateway import GatewayError, GatewayPayment
from app.routers import internal

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="reconciling reads and writes the database"
)

TENANT = "JEENE_MASTER"
STUDENT = "test-reconcile-student"
SECRET = "a-shared-secret-only-cron-knows"


def _sql(statement, *args):
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await db._init_connection(conn)
        try:
            return await conn.fetch(statement, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


class StubGateway:
    """Answers about specific orders, and can be unreachable for specific orders."""

    def __init__(self) -> None:
        self.attempts: dict[str, list[GatewayPayment]] = {}
        self.unreachable: set[str] = set()
        self.asked: list[str] = []

    def says(self, order_id: str, status: str, *, reason: str = "") -> None:
        self.attempts[order_id] = [GatewayPayment(
            payment_id=f"pay_{uuid.uuid4().hex[:8]}", order_id=order_id, status=status,
            amount_paise=19900, failure_reason=reason,
            raw={"status": status},
        )]

    async def fetch_order_payments(self, order_id):
        self.asked.append(order_id)
        if order_id in self.unreachable:
            raise GatewayError("gateway down")
        return self.attempts.get(order_id, [])

    async def create_order(self, **kwargs):  # pragma: no cover
        raise AssertionError("the reconciler never creates an order")

    async def fetch_payment(self, payment_id):  # pragma: no cover
        raise AssertionError("the reconciler asks about orders, not payments")

    def verify_payment_signature(self, **kwargs):  # pragma: no cover
        raise AssertionError("nothing is signed on this path")

    def verify_webhook_signature(self, **kwargs):  # pragma: no cover
        raise AssertionError("nothing is signed on this path")


@pytest.fixture(scope="module", autouse=True)
def student():
    _sql(
        """INSERT INTO users (firebase_uid, tenant_id) VALUES ($1, $2)
           ON CONFLICT (firebase_uid) DO NOTHING""",
        STUDENT, TENANT,
    )
    yield
    _sql("DELETE FROM entitlement_grants WHERE firebase_uid = $1", STUDENT)
    _sql("DELETE FROM payments WHERE firebase_uid = $1", STUDENT)
    _sql("DELETE FROM users WHERE firebase_uid = $1", STUDENT)


@pytest.fixture(scope="module")
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def gw(monkeypatch):
    from app import billing
    from app.config import settings

    monkeypatch.setattr(settings, "jeene_reconcile_secret", SECRET, raising=False)
    stub = StubGateway()
    billing.set_gateway(stub)
    # Everything else pending in this database is aged out of the window, so each test
    # sweeps exactly the rows it made.
    _sql("UPDATE payments SET created_at = now() WHERE status = 'pending'")
    yield stub
    billing.set_gateway(None)


def _pending(minutes_old: float) -> str:
    """A payment that has been waiting this long. Returns its order id."""
    product_id = f"test_prod_{uuid.uuid4().hex[:8]}"
    order_id = f"order_test_{uuid.uuid4().hex[:12]}"
    _sql(
        """INSERT INTO products (product_id, tenant_id, title, amount_paise, duration_days)
           VALUES ($1, $2, 'Test Pass', 19900, 30)""",
        product_id, TENANT,
    )
    _sql(
        """INSERT INTO payments (order_id, firebase_uid, tenant_id, product_id,
                                 amount_paise, currency, duration_days, created_at)
           VALUES ($1, $2, $3, $4, 19900, 'INR', 30, now() - make_interval(mins => $5))""",
        order_id, STUDENT, TENANT, product_id, minutes_old,
    )
    return order_id


def _sweep(client, secret: str = SECRET):
    return client.post(
        "/internal/billing/reconcile", headers={"X-Jeene-Reconcile": secret}
    )


def _status(order_id: str) -> str:
    return _sql("SELECT status FROM payments WHERE order_id = $1", order_id)[0]["status"]


def _grants(order_id: str) -> int:
    return len(_sql("SELECT 1 FROM entitlement_grants WHERE order_id = $1", order_id))


# --- who may run it -------------------------------------------------------------------


def test_a_sweep_without_the_secret_is_forbidden(client, gw):
    assert client.post("/internal/billing/reconcile").status_code == 403
    assert _sweep(client, "not-the-secret").status_code == 403
    assert gw.asked == []


def test_a_deployment_without_a_configured_secret_refuses_to_sweep(client, monkeypatch):
    """An endpoint that grants entitlements must not be open when a variable is missing."""
    from app.config import settings

    monkeypatch.setattr(settings, "jeene_reconcile_secret", None, raising=False)
    assert _sweep(client, "anything").status_code == 503


def test_the_reconciler_is_not_in_the_public_schema(client):
    """Nothing in the app calls it, and it is not documentation anybody should read."""
    route = next(
        r for r in internal.router.routes if r.path == "/internal/billing/reconcile"
    )
    assert route.include_in_schema is False


# --- what it does ---------------------------------------------------------------------


def test_a_captured_payment_nobody_told_us_about_is_settled_and_granted(client, gw):
    """The case the reconciler exists for: the phone died on the success screen."""
    order = _pending(minutes_old=10)
    gw.says(order, "captured")

    report = _sweep(client).json()

    assert report["paid"] == 1
    assert _status(order) == "paid"
    assert _grants(order) == 1


def test_a_payment_still_inside_its_grace_period_is_left_alone(client, gw):
    """Its own three confirmation paths get first refusal. No race for no gain."""
    order = _pending(minutes_old=1)

    report = _sweep(client).json()

    assert report["examined"] == 0
    assert gw.asked == [], "the gateway is not asked about a checkout still in progress"
    assert _status(order) == "pending"


def test_a_payment_with_no_attempts_yet_stays_pending(client, gw):
    """Past the grace period but not abandoned. Somebody may still be typing an OTP."""
    order = _pending(minutes_old=10)
    gw.attempts[order] = []

    report = _sweep(client).json()

    assert report["left_pending"] == 1
    assert report["failed"] == 0
    assert _status(order) == "pending"


def test_an_abandoned_payment_is_failed_so_the_student_can_try_again(client, gw):
    order = _pending(minutes_old=45)
    gw.attempts[order] = []

    report = _sweep(client).json()

    assert report["failed"] == 1
    assert _status(order) == "failed"
    assert _grants(order) == 0


def test_a_declined_payment_keeps_the_reason_the_gateway_gave(client, gw):
    order = _pending(minutes_old=45)
    gw.says(order, "failed", reason="the card was declined")

    _sweep(client)

    row = _sql("SELECT status, failure_reason FROM payments WHERE order_id = $1", order)[0]
    assert row["status"] == "failed"
    assert row["failure_reason"] == "the card was declined"


def test_money_held_but_never_taken_goes_to_a_person(client, gw):
    """Auto-capture is on, so this should not happen. When it does, a timer cannot fix it."""
    order = _pending(minutes_old=45)
    gw.says(order, "authorized")

    report = _sweep(client).json()

    assert report["review"] == 1
    assert _status(order) == "needs_manual_review"
    assert _grants(order) == 0


def test_a_recent_authorisation_is_given_time_to_capture(client, gw):
    order = _pending(minutes_old=10)
    gw.says(order, "authorized")

    report = _sweep(client).json()

    assert report["left_pending"] == 1
    assert _status(order) == "pending"


def test_one_unreachable_order_does_not_end_the_sweep(client, gw):
    reachable = _pending(minutes_old=10)
    broken = _pending(minutes_old=10)
    gw.says(reachable, "captured")
    gw.unreachable.add(broken)

    report = _sweep(client).json()

    assert report["examined"] == 2
    assert report["paid"] == 1
    assert report["left_pending"] == 1
    assert _status(reachable) == "paid"
    assert _status(broken) == "pending", "asked about again on the next sweep"


def test_sweeping_twice_grants_access_once(client, gw):
    order = _pending(minutes_old=10)
    gw.says(order, "captured")

    first = _sweep(client).json()
    second = _sweep(client).json()

    assert first["paid"] == 1
    assert second["examined"] == 0, "it is no longer pending, so it is no longer swept"
    assert _grants(order) == 1
