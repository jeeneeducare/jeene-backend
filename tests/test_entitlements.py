"""What a student's grants add up to.

Every assertion here is about money already taken. Three of them describe behaviour a
student would notice and complain about, and they are the reason the ledger is folded
rather than summed:

  * **stacking** — buying again while still subscribed must extend, not restart;
  * **lapsing** — coming back after expiry must start from today, not from the old
    expiry, or the student pays for weeks in which they had nothing;
  * **idempotency** — the same payment must grant access exactly once, no matter how
    many of the four confirmation paths fire.

The last one is enforced by a unique index rather than by code, so it is tested against a
real database. These tests need Postgres: `DATABASE_URL` pointing at the local
`jeene_test`, as the rest of the integration suite does.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest

from app.billing import entitlements

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="entitlement maths is enforced by database constraints, so it needs a database",
)

TENANT = "JEENE_MASTER"


def in_tx(body):
    """Run `body(conn)` in a transaction that is always rolled back.

    Sync tests calling `asyncio.run`, matching the rest of the suite. The rollback is the
    point: these tests insert users and grants into a database that has other data in it,
    and a grant left behind would be folded into the next test's expiry.
    """
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        tx = conn.transaction()
        await tx.start()
        try:
            return await body(conn)
        finally:
            await tx.rollback()
            await conn.close()

    return asyncio.run(go())


async def _student(conn) -> str:
    uid = f"test-{uuid.uuid4()}"
    await conn.execute(
        "INSERT INTO users (firebase_uid, tenant_id) VALUES ($1, $2)", uid, TENANT
    )
    return uid


async def _grant_at(conn, uid: str, days: int, when: datetime, order_id=None) -> None:
    """Insert a grant with an explicit timestamp, so a whole history fits in one test."""
    await conn.execute(
        """
        INSERT INTO entitlement_grants
               (firebase_uid, tenant_id, tier, days, provider, order_id, granted_at)
        VALUES ($1, $2, 'pro', $3, 'test', $4, $5)
        """,
        uid, TENANT, days, order_id, when,
    )


# --- the shape of access ------------------------------------------------------------


def test_a_student_with_no_grants_has_never_had_access():
    async def body(conn):
        uid = await _student(conn)
        assert await entitlements.recompute(conn, uid, TENANT) is None

        held = await entitlements.entitlement_of(conn, uid, TENANT)
        assert held.active is False
        assert held.expires_at is None
        assert held.days_remaining == 0

    in_tx(body)


def test_one_purchase_runs_from_the_moment_it_was_paid():
    async def body(conn):
        uid = await _student(conn)
        bought = datetime(2026, 1, 1, tzinfo=timezone.utc)
        await _grant_at(conn, uid, 30, bought)

        assert await entitlements.recompute(conn, uid, TENANT) == bought + timedelta(days=30)

    in_tx(body)


# --- the two behaviours a student would notice ---------------------------------------


def test_buying_again_while_subscribed_extends_rather_than_restarts():
    async def body(conn):
        """Thirty days, then ninety more a fortnight in, is 120 days from the first — not 90.

        Restarting would quietly take the fortnight they had already paid for.
        """
        uid = await _student(conn)
        first = datetime(2026, 1, 1, tzinfo=timezone.utc)
        await _grant_at(conn, uid, 30, first)
        await _grant_at(conn, uid, 90, first + timedelta(days=14))

        assert await entitlements.recompute(conn, uid, TENANT) == first + timedelta(days=120)

    in_tx(body)


def test_coming_back_after_a_lapse_starts_from_today():
    async def body(conn):
        """The failure mode of summing: sixty days from January for somebody who was away.

        Bought 30 days on 1 January, expired 31 January, bought 30 more on 15 February. They
        are owed until 17 March. "Sixty days from the first grant" would say 2 March and
        silently charge them for the fortnight they spent locked out.
        """
        uid = await _student(conn)
        january = datetime(2026, 1, 1, tzinfo=timezone.utc)
        february = datetime(2026, 2, 15, tzinfo=timezone.utc)
        await _grant_at(conn, uid, 30, january)
        await _grant_at(conn, uid, 30, february)

        assert await entitlements.recompute(conn, uid, TENANT) == february + timedelta(days=30)

    in_tx(body)


def test_a_refund_shortens_access_without_editing_history():
    async def body(conn):
        uid = await _student(conn)
        bought = datetime(2026, 1, 1, tzinfo=timezone.utc)
        await _grant_at(conn, uid, 90, bought)
        await _grant_at(conn, uid, -60, bought + timedelta(days=1))

        assert await entitlements.recompute(conn, uid, TENANT) == bought + timedelta(days=30)
        # The reversal is a row, not a deletion: both are still on the record.
        assert await conn.fetchval(
            "SELECT count(*) FROM entitlement_grants WHERE firebase_uid = $1", uid
        ) == 2

    in_tx(body)


# --- idempotency, which is the whole reason for the ledger ---------------------------


def test_the_same_payment_grants_access_exactly_once():
    async def body(conn):
        """Four confirmation paths race on every purchase. Only one may add days."""
        uid = await _student(conn)
        order = f"order_{uuid.uuid4().hex[:12]}"
        await conn.execute(
            """
            INSERT INTO products (product_id, tenant_id, title, amount_paise, duration_days)
            VALUES ('test_monthly', $1, 'Test', 19900, 30)
            ON CONFLICT (product_id) DO NOTHING
            """,
            TENANT,
        )
        await conn.execute(
            """
            INSERT INTO payments (order_id, firebase_uid, tenant_id, product_id,
                                  amount_paise, currency, duration_days, status)
            VALUES ($1, $2, $3, 'test_monthly', 19900, 'INR', 30, 'paid')
            """,
            order, uid, TENANT,
        )

        first = await entitlements.grant(
            conn, uid=uid, tenant=TENANT, days=30, provider="razorpay", order_id=order
        )
        second = await entitlements.grant(
            conn, uid=uid, tenant=TENANT, days=30, provider="razorpay", order_id=order
        )

        assert first is True, "the first path to arrive grants the days"
        assert second is False, "every later path is a no-op, not a second thirty days"
        assert await conn.fetchval(
            "SELECT count(*) FROM entitlement_grants WHERE order_id = $1", order
        ) == 1

    in_tx(body)


def test_grants_without_an_order_are_not_deduplicated():
    async def body(conn):
        """Two support comps are two comps. The uniqueness is on the payment, not the user."""
        uid = await _student(conn)
        assert await entitlements.grant(
            conn, uid=uid, tenant=TENANT, days=7, provider="manual", note="apology"
        )
        assert await entitlements.grant(
            conn, uid=uid, tenant=TENANT, days=7, provider="manual", note="apology again"
        )
        assert await conn.fetchval(
            "SELECT count(*) FROM entitlement_grants WHERE firebase_uid = $1", uid
        ) == 2

    in_tx(body)


# --- the cache -----------------------------------------------------------------------


def test_granting_writes_the_cache_in_the_same_breath():
    async def body(conn):
        uid = await _student(conn)
        await entitlements.grant(
            conn, uid=uid, tenant=TENANT, days=30, provider="manual"
        )
        cached = await conn.fetchval(
            "SELECT pro_expires_at FROM users WHERE firebase_uid = $1", uid
        )
        assert cached is not None
        assert cached == await entitlements.recompute(conn, uid, TENANT)

    in_tx(body)


def test_the_cache_is_filled_in_for_a_student_who_predates_it():
    async def body(conn):
        """A grant written before the column existed leaves a null there and a good ledger."""
        uid = await _student(conn)
        await _grant_at(conn, uid, 30, datetime(2026, 1, 1, tzinfo=timezone.utc))
        await conn.execute(
            "UPDATE users SET pro_expires_at = NULL WHERE firebase_uid = $1", uid
        )

        held = await entitlements.entitlement_of(conn, uid, TENANT)
        assert held.expires_at is not None
        assert await conn.fetchval(
            "SELECT pro_expires_at FROM users WHERE firebase_uid = $1", uid
        ) == held.expires_at

    in_tx(body)


# --- the boundary --------------------------------------------------------------------


def test_access_ends_at_the_expiry_and_not_a_second_later():
    async def body(conn):
        uid = await _student(conn)
        now = datetime.now(timezone.utc)

        await _grant_at(conn, uid, 30, now - timedelta(days=29, hours=23))
        assert (await entitlements.entitlement_of(conn, uid, TENANT)).active is True

        await conn.execute("DELETE FROM entitlement_grants WHERE firebase_uid = $1", uid)
        await _grant_at(conn, uid, 30, now - timedelta(days=30, minutes=1))
        await entitlements.refresh_cache(conn, uid, TENANT)
        assert (await entitlements.entitlement_of(conn, uid, TENANT)).active is False

    in_tx(body)


def test_days_remaining_is_what_the_app_prints():
    async def body(conn):
        uid = await _student(conn)
        await _grant_at(conn, uid, 30, datetime.now(timezone.utc) - timedelta(days=18))
        await entitlements.refresh_cache(conn, uid, TENANT)

        held = await entitlements.entitlement_of(conn, uid, TENANT)
        assert held.days_remaining == 11, "floored, so it never promises a day that is going"

    in_tx(body)


# --- two payments landing together -------------------------------------------------


def test_two_purchases_settling_at_once_are_both_counted():
    """The bug this file's locking exists for, and it was silent.

    Two payments by one student settle against two *different* `payments` rows, so the
    row lock in `settle` does not serialise them against each other. Without a lock on
    the student, both folded a ledger that did not yet contain the other's grant and the
    second write landed on top of the first: the ledger said sixty days, the cache said
    thirty, and every gate reads the cache.

    Real connections and real transactions, because the failure is a race and an
    in-transaction fake cannot have one.
    """
    async def go():
        setup = await asyncpg.connect(os.environ["DATABASE_URL"])
        uid = f"test-{uuid.uuid4()}"
        try:
            await setup.execute(
                "INSERT INTO users (firebase_uid, tenant_id) VALUES ($1, $2)", uid, TENANT
            )

            async def one(days: int):
                conn = await asyncpg.connect(os.environ["DATABASE_URL"])
                try:
                    async with conn.transaction():
                        await entitlements.grant(
                            conn, uid=uid, tenant=TENANT, days=days, provider="test"
                        )
                finally:
                    await conn.close()

            await asyncio.gather(one(30), one(30))

            cached = await setup.fetchval(
                "SELECT pro_expires_at FROM users WHERE firebase_uid = $1", uid
            )
            folded = await entitlements.recompute(setup, uid, TENANT)
            assert cached == folded, "the cache every gate reads must match the ledger"
            assert (folded - datetime.now(timezone.utc)).days >= 59, "sixty days, not thirty"
        finally:
            await setup.execute("DELETE FROM entitlement_grants WHERE firebase_uid = $1", uid)
            await setup.execute("DELETE FROM users WHERE firebase_uid = $1", uid)
            await setup.close()

    asyncio.run(go())


def test_a_free_student_costs_one_query_to_check():
    """Not a micro-optimisation: this runs on every gated request for every free student.

    A null cache means both "never subscribed" and "predates the column", so it cannot be
    filled in for somebody with no grants — which left the fold running for ever on the
    commonest path in the app. Asking for both facts at once settles it in one round trip.
    """
    class Counting:
        """The real connection, plus a tally. asyncpg's own methods cannot be replaced."""

        def __init__(self, real):
            self._real = real
            self.statements: list[str] = []

        def __getattr__(self, name):
            attribute = getattr(self._real, name)
            if name not in ("fetch", "fetchrow", "fetchval", "execute"):
                return attribute

            async def counted(statement, *args, **kwargs):
                self.statements.append(" ".join(statement.split())[:60])
                return await attribute(statement, *args, **kwargs)

            return counted

    async def body(conn):
        uid = await _student(conn)
        watched = Counting(conn)

        held = await entitlements.entitlement_of(watched, uid, TENANT)

        assert held.active is False
        assert len(watched.statements) == 1, (
            f"one statement, not {len(watched.statements)}: {watched.statements}"
        )

    in_tx(body)
