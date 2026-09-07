"""The state machine every confirmation path goes through.

Four independent things can confirm the same payment — the app's success callback, a
webhook, a failure report, the reconciler — and they routinely arrive together. Each one
calls [app.billing.settle.settle] and nothing else writes `payments.status`, so this file
is where "confirmed twice" is proven to mean the same as "confirmed once".

The transitions are asserted in both directions, because the interesting ones are the
backwards ones. Two of them are rules rather than mechanics and are the reason this is a
four-state machine rather than three:

  * **a failure report cannot un-pay a payment.** Money is in; a stale webhook or a
    patched client saying otherwise must not revoke access somebody paid for.
  * **a capture landing on a failed payment goes to a person.** By then the student has
    been told it did not work and may well have paid again, so quietly granting access is
    the one outcome nobody can act on.

Real database, because the thing being tested is partly a unique index: one grant per
order, enforced by Postgres rather than by the code asking nicely.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest

from app import db
from app.billing import entitlements, settle

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="settling is idempotent because of a database constraint, so it needs one",
)

TENANT = "JEENE_MASTER"


def in_tx(body):
    """Run `body(conn)` in a transaction that is always rolled back.

    Same shape as the entitlement tests: sync test, `asyncio.run`, nothing left behind.
    A payment or a grant that survived would be folded into the next test's expiry.

    The connection is given the pool's jsonb codec, so `gateway_payload` comes back as a
    dict here exactly as it does in a request. Without it these tests would read a string
    and quietly pass on assertions the running server never makes.
    """
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await db._init_connection(conn)
        tx = conn.transaction()
        await tx.start()
        try:
            return await body(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(go())


async def _pending(conn, *, days: int = 30) -> tuple[str, str]:
    """A student, a product and a payment waiting on an answer. Returns (uid, order_id)."""
    uid = f"test-{uuid.uuid4()}"
    product_id = f"test_prod_{uuid.uuid4().hex[:8]}"
    order_id = f"order_test_{uuid.uuid4().hex[:12]}"

    await conn.execute(
        "INSERT INTO users (firebase_uid, tenant_id) VALUES ($1, $2)", uid, TENANT
    )
    await conn.execute(
        """INSERT INTO products (product_id, tenant_id, title, amount_paise, duration_days)
           VALUES ($1, $2, 'Test Pass', 19900, $3)""",
        product_id, TENANT, days,
    )
    await conn.execute(
        """INSERT INTO payments (order_id, firebase_uid, tenant_id, product_id,
                                 amount_paise, currency, duration_days)
           VALUES ($1, $2, $3, $4, 19900, 'INR', $5)""",
        order_id, uid, TENANT, product_id, days,
    )
    return uid, order_id


async def _row(conn, order_id: str) -> asyncpg.Record:
    return await conn.fetchrow("SELECT * FROM payments WHERE order_id = $1", order_id)


async def _grants(conn, uid: str) -> list[asyncpg.Record]:
    return await conn.fetch(
        "SELECT * FROM entitlement_grants WHERE firebase_uid = $1", uid
    )


# --- forwards ---------------------------------------------------------------------------


def test_a_captured_payment_is_paid_and_grants_access():
    def go():
        async def body(conn):
            uid, order_id = await _pending(conn, days=30)

            outcome = await settle.settle(
                conn, order_id, to=settle.PAID, payment_id="pay_abc",
                payload={"id": "pay_abc", "status": "captured"},
            )

            assert (outcome.status, outcome.changed, outcome.granted) == ("paid", True, True)

            row = await _row(conn, order_id)
            assert row["status"] == "paid"
            assert row["razorpay_payment_id"] == "pay_abc"
            assert row["settled_at"] is not None
            assert row["gateway_payload"]["status"] == "captured"

            grants = await _grants(conn, uid)
            assert len(grants) == 1
            assert grants[0]["days"] == 30
            assert grants[0]["provider"] == "razorpay"
            assert grants[0]["order_id"] == order_id

            held = await entitlements.entitlement_of(conn, uid, TENANT)
            assert held.active is True
        return body
    in_tx(go())


def test_a_failed_payment_records_the_reason_and_grants_nothing():
    def go():
        async def body(conn):
            uid, order_id = await _pending(conn)

            outcome = await settle.settle(
                conn, order_id, to=settle.FAILED,
                failure_reason="the card was declined",
            )

            assert (outcome.status, outcome.changed, outcome.granted) == (
                "failed", True, False
            )
            row = await _row(conn, order_id)
            assert row["status"] == "failed"
            assert row["failure_reason"] == "the card was declined"
            assert await _grants(conn, uid) == []

            held = await entitlements.entitlement_of(conn, uid, TENANT)
            assert held.active is False
        return body
    in_tx(go())


def test_the_days_come_from_the_payment_and_not_from_the_product_today():
    """A price or duration change must not rewrite what somebody already bought."""
    def go():
        async def body(conn):
            uid, order_id = await _pending(conn, days=90)
            await conn.execute(
                """UPDATE products SET duration_days = 365
                    WHERE product_id = (SELECT product_id FROM payments WHERE order_id = $1)""",
                order_id,
            )

            await settle.settle(conn, order_id, to=settle.PAID, payment_id="pay_x")

            grants = await _grants(conn, uid)
            assert grants[0]["days"] == 90
        return body
    in_tx(go())


# --- arriving second --------------------------------------------------------------------


def test_the_same_confirmation_five_times_grants_access_once():
    """A webhook redelivered is the normal case, not an error. Five is Razorpay's retry."""
    def go():
        async def body(conn):
            uid, order_id = await _pending(conn)

            outcomes = [
                await settle.settle(conn, order_id, to=settle.PAID, payment_id="pay_abc")
                for _ in range(5)
            ]

            assert [o.changed for o in outcomes] == [True, False, False, False, False]
            assert [o.granted for o in outcomes] == [True, False, False, False, False]
            assert all(o.status == "paid" for o in outcomes)
            assert len(await _grants(conn, uid)) == 1
        return body
    in_tx(go())


def test_a_second_failure_report_changes_nothing():
    def go():
        async def body(conn):
            _, order_id = await _pending(conn)
            await settle.settle(conn, order_id, to=settle.FAILED, failure_reason="cancelled")

            outcome = await settle.settle(
                conn, order_id, to=settle.FAILED, failure_reason="something else"
            )

            assert (outcome.status, outcome.changed) == ("failed", False)
            row = await _row(conn, order_id)
            assert row["failure_reason"] == "cancelled", "the first reason is the real one"
        return body
    in_tx(go())


def test_settling_again_does_not_move_settled_at():
    """The receipt says when the money moved, not when we last heard about it."""
    def go():
        async def body(conn):
            _, order_id = await _pending(conn)
            await settle.settle(conn, order_id, to=settle.PAID, payment_id="pay_abc")
            first = (await _row(conn, order_id))["settled_at"]

            await settle.settle(conn, order_id, to=settle.PAID, payment_id="pay_abc")
            row = await _row(conn, order_id)

            assert row["settled_at"] == first
            assert row["updated_at"] >= first, "but we do record that we heard again"
        return body
    in_tx(go())


# --- backwards --------------------------------------------------------------------------


def test_a_failure_report_cannot_un_pay_a_payment():
    """The one a patched client would try: pay, then report failure and keep the access."""
    def go():
        async def body(conn):
            uid, order_id = await _pending(conn)
            await settle.settle(conn, order_id, to=settle.PAID, payment_id="pay_abc")

            outcome = await settle.settle(
                conn, order_id, to=settle.FAILED, failure_reason="user says it failed"
            )

            assert (outcome.status, outcome.changed) == ("paid", False)
            assert (await _row(conn, order_id))["status"] == "paid"
            assert len(await _grants(conn, uid)) == 1

            held = await entitlements.entitlement_of(conn, uid, TENANT)
            assert held.active is True, "access somebody paid for is never revoked here"
        return body
    in_tx(go())


def test_a_capture_after_we_gave_up_goes_to_a_person_not_to_paid():
    """The race the fourth state exists for.

    The student has already been told it did not work. Granting quietly would leave a
    double payment nobody notices; failing it keeps money we took. Neither is automatable.
    """
    def go():
        async def body(conn):
            uid, order_id = await _pending(conn)
            await settle.settle(
                conn, order_id, to=settle.FAILED, failure_reason="not completed in time"
            )

            outcome = await settle.settle(
                conn, order_id, to=settle.PAID, payment_id="pay_late",
                payload={"id": "pay_late", "status": "captured"},
            )

            assert outcome.status == "needs_manual_review"
            assert outcome.changed is True
            assert outcome.granted is False

            row = await _row(conn, order_id)
            assert row["status"] == "needs_manual_review"
            assert row["razorpay_payment_id"] == "pay_late", "the payment id a human needs"
            assert row["gateway_payload"]["id"] == "pay_late"

            assert await _grants(conn, uid) == [], "a person decides, not this code"
        return body
    in_tx(go())


def test_manual_review_is_not_resolved_by_another_automated_confirmation():
    """Once a human is looking at it, nothing automated closes it behind their back."""
    def go():
        async def body(conn):
            uid, order_id = await _pending(conn)
            await settle.settle(conn, order_id, to=settle.FAILED, failure_reason="gave up")
            await settle.settle(conn, order_id, to=settle.PAID, payment_id="pay_late")

            again = await settle.settle(conn, order_id, to=settle.PAID, payment_id="pay_late")
            assert (again.status, again.changed) == ("needs_manual_review", False)

            and_a_failure = await settle.settle(conn, order_id, to=settle.FAILED)
            assert (and_a_failure.status, and_a_failure.changed) == (
                "needs_manual_review", False
            )
            assert await _grants(conn, uid) == []
        return body
    in_tx(go())


def test_review_can_be_reached_directly_for_an_uncaptured_authorisation():
    """What the reconciler does with money that is held but never taken."""
    def go():
        async def body(conn):
            uid, order_id = await _pending(conn)

            outcome = await settle.settle(
                conn, order_id, to=settle.REVIEW, payment_id="pay_held",
                failure_reason="authorised but never captured",
            )

            assert (outcome.status, outcome.changed, outcome.granted) == (
                "needs_manual_review", True, False
            )
            assert await _grants(conn, uid) == []
        return body
    in_tx(go())


# --- refusals ---------------------------------------------------------------------------


def test_settling_an_order_we_never_created_is_an_error_not_a_row():
    """A webhook aimed at the wrong environment must not invent a payment."""
    def go():
        async def body(conn):
            with pytest.raises(settle.UnknownOrder):
                await settle.settle(conn, "order_that_never_existed", to=settle.PAID)
        return body
    in_tx(go())


def test_pending_is_not_something_a_payment_can_be_settled_to():
    """Settling means finishing. Nothing may push a payment back into the queue."""
    def go():
        async def body(conn):
            _, order_id = await _pending(conn)
            with pytest.raises(ValueError):
                await settle.settle(conn, order_id, to=settle.PENDING)
            assert (await _row(conn, order_id))["status"] == "pending"
        return body
    in_tx(go())


# --- an attempt failing, which is not the payment failing ---------------------------------


def test_a_failed_attempt_keeps_its_reason_and_leaves_the_payment_open():
    """One order can be paid at the second attempt. The first must not close it."""
    def go():
        async def body(conn):
            uid, order_id = await _pending(conn)

            noted = await settle.record_failed_attempt(
                conn, order_id, payment_id="pay_try1", reason="declined",
                payload={"id": "pay_try1", "status": "failed"},
            )

            assert noted is True
            row = await _row(conn, order_id)
            assert row["status"] == "pending", "still payable"
            assert row["failure_reason"] == "declined"
            assert row["settled_at"] is None
            assert await _grants(conn, uid) == []

            # And the retry settles normally, rather than as a late capture.
            outcome = await settle.settle(conn, order_id, to=settle.PAID,
                                          payment_id="pay_try2")
            assert outcome.status == "paid"
            assert len(await _grants(conn, uid)) == 1
        return body
    in_tx(go())


def test_a_failed_attempt_cannot_disturb_a_payment_that_is_already_decided():
    def go():
        async def body(conn):
            _, order_id = await _pending(conn)
            await settle.settle(conn, order_id, to=settle.PAID, payment_id="pay_ok")

            noted = await settle.record_failed_attempt(
                conn, order_id, payment_id="pay_late", reason="declined"
            )

            assert noted is False
            row = await _row(conn, order_id)
            assert row["status"] == "paid"
            assert row["failure_reason"] == ""
            assert row["razorpay_payment_id"] == "pay_ok"
        return body
    in_tx(go())


# --- less money than we charged -----------------------------------------------------------


def test_a_short_payment_is_not_a_paid_payment():
    """Partial payments are not enabled, so this cannot happen — which is why it is here.

    Granting a year of access for part of a year's price is the one mistake that cannot be
    undone by noticing it later.
    """
    def go():
        async def body(conn):
            uid, order_id = await _pending(conn)

            outcome = await settle.settle(
                conn, order_id, to=settle.PAID, payment_id="pay_short",
                paid_paise=100,
            )

            assert outcome.status == "needs_manual_review"
            assert outcome.granted is False
            row = await _row(conn, order_id)
            assert row["status"] == "needs_manual_review"
            assert "100 of 19900 paise" in row["failure_reason"]
            assert await _grants(conn, uid) == []
        return body
    in_tx(go())


def test_paying_the_full_amount_settles_normally():
    def go():
        async def body(conn):
            uid, order_id = await _pending(conn)

            outcome = await settle.settle(
                conn, order_id, to=settle.PAID, payment_id="pay_ok", paid_paise=19900,
            )

            assert outcome.status == "paid"
            assert len(await _grants(conn, uid)) == 1
        return body
    in_tx(go())
