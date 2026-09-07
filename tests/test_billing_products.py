"""The paywall's data, and the answer to "am I Pro".

The read-only half of billing. Nothing here moves money — the assertions that matter are
about what leaks and what an admin can do by accident:

  * a price is an **integer** everywhere, because a float in this path eventually rounds
    somebody's charge the wrong way;
  * products are **public**, because a paywall you must sign up to read is a paywall
    nobody signs up for — and the reply must therefore contain nothing about anybody;
  * retiring a product **hides it without deleting it**, because payments reference
    products forever and a receipt whose product has vanished cannot be explained to the
    person who paid.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="billing reads from the database",
)

TENANT = "JEENE_MASTER"
ADMIN_UID = "test-billing-admin"


def _sql(*statements):
    """Run statements against the test database, outside any request."""
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            for statement, args in statements:
                await conn.execute(statement, *args)
        finally:
            await conn.close()
    asyncio.run(go())


@pytest.fixture(scope="module")
def client():
    from app.auth import current_tenant, optional_user, require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: {"uid": "student-fresh"}
    app.dependency_overrides[optional_user] = lambda: {"uid": "student-fresh"}
    app.dependency_overrides[current_tenant] = lambda: TENANT
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(scope="module")
def admin_client():
    """A caller the `admins` table vouches for."""
    from app.auth import current_tenant, require_admin, require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: {"uid": ADMIN_UID}
    app.dependency_overrides[current_tenant] = lambda: TENANT
    app.dependency_overrides[require_admin] = lambda: {
        "uid": ADMIN_UID, "tenant_id": TENANT, "admin_email": "test@localhost"
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def a_product():
    """A product that exists for one test and is gone afterwards."""
    product_id = f"test_prod_{uuid.uuid4().hex[:8]}"
    _sql((
        """INSERT INTO products (product_id, tenant_id, title, amount_paise,
                                 duration_days, badge, sort_order)
           VALUES ($1, $2, 'Test Monthly', 19900, 30, 'Most Popular', 1)""",
        (product_id, TENANT),
    ))
    yield product_id
    _sql(("DELETE FROM products WHERE product_id = $1", (product_id,)))


# --- what a student sees --------------------------------------------------------------


def test_prices_reach_the_app_as_integer_paise(client, a_product):
    """₹199 is 19900, not 199.0. No float anywhere on this path.

    A rupee float would be fine right up until a runtime rounded 1199.995 * 100 to
    119999.49999999999 and two amounts stopped comparing equal.
    """
    body = client.get("/billing/products").json()
    mine = next(p for p in body if p["product_id"] == a_product)

    assert mine["amount_paise"] == 19900
    assert isinstance(mine["amount_paise"], int)
    assert mine["duration_days"] == 30
    assert mine["currency"] == "INR"


def test_the_paywall_is_readable_before_signing_in(client, a_product):
    """Products are public. Somebody deciding whether to make an account can see the price."""
    from app.auth import optional_user, require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: None
    app.dependency_overrides[optional_user] = lambda: None
    try:
        response = client.get("/billing/products")
    finally:
        app.dependency_overrides[require_user] = lambda: {"uid": "student-fresh"}
        app.dependency_overrides[optional_user] = lambda: {"uid": "student-fresh"}

    assert response.status_code == 200
    assert any(p["product_id"] == a_product for p in response.json())


def test_the_product_list_says_nothing_about_anybody(client, a_product):
    """It is served to anonymous callers, so it must carry no personal field at all."""
    body = client.get("/billing/products").json()
    leaks = {"firebase_uid", "uid", "email", "phone", "user", "expires_at"}
    for product in body:
        assert leaks.isdisjoint(product.keys()), product


def test_a_retired_product_disappears_from_the_paywall(client, a_product):
    _sql(("UPDATE products SET active = FALSE WHERE product_id = $1", (a_product,)))
    body = client.get("/billing/products").json()
    assert all(p["product_id"] != a_product for p in body)


# --- what the app asks after a purchase ------------------------------------------------


def test_a_student_who_has_never_paid_is_not_pro(client):
    body = client.get("/billing/me").json()
    assert body["active"] is False
    assert body["expires_at"] is None
    assert body["days_remaining"] == 0
    assert body["tier"] == "pro"


def test_active_is_decided_by_the_server_not_the_device(client):
    """The reply carries a verdict, not just a date.

    A phone with a wrong clock — or a deliberately wrong one — must not be able to
    unlock anything by comparing `expires_at` itself.
    """
    body = client.get("/billing/me").json()
    assert "active" in body, "the app must never have to derive this"
    assert isinstance(body["active"], bool)


# --- what an admin can and cannot do ---------------------------------------------------


def test_an_admin_can_create_and_amend_a_product(admin_client):
    product_id = f"test_prod_{uuid.uuid4().hex[:8]}"
    try:
        created = admin_client.post("/admin/products", json={
            "product_id": product_id, "title": "Quarterly",
            "amount_paise": 49900, "duration_days": 90, "badge": "Save 16%",
        })
        assert created.status_code == 200, created.text
        assert created.json()["amount_paise"] == 49900

        amended = admin_client.post("/admin/products", json={
            "product_id": product_id, "title": "Quarterly",
            "amount_paise": 44900, "duration_days": 90,
        })
        assert amended.status_code == 200
        assert amended.json()["amount_paise"] == 44900
    finally:
        _sql(("DELETE FROM products WHERE product_id = $1", (product_id,)))


def test_a_price_of_nothing_is_refused_with_a_sentence(admin_client):
    response = admin_client.post("/admin/products", json={
        "product_id": "test_free", "title": "Free", "amount_paise": 0, "duration_days": 30,
    })
    assert response.status_code == 400
    assert "more than nothing" in response.json()["detail"]


def test_a_pass_lasting_no_time_is_refused(admin_client):
    response = admin_client.post("/admin/products", json={
        "product_id": "test_instant", "title": "Instant",
        "amount_paise": 100, "duration_days": 0,
    })
    assert response.status_code == 400


def test_retiring_hides_a_product_without_deleting_it(admin_client, a_product):
    """Payments reference products forever. A delete would orphan somebody's receipt."""
    response = admin_client.delete(f"/admin/products/{a_product}")
    assert response.status_code == 200
    assert response.json()["active"] is False

    still_there = admin_client.get("/admin/products").json()
    assert any(p["product_id"] == a_product for p in still_there), \
        "the admin list keeps retired products so one can be brought back"


def test_product_writes_are_behind_require_admin():
    """A valid token is not the check and never can be.

    Every student who has opened the app once holds one. Asserted against the route's
    dependencies rather than by calling it with a student token, which is how the rest of
    the suite pins this and avoids standing up a second client to prove a negative.
    """
    import inspect

    from app.auth import require_admin
    from app.routers import admin as admin_router

    for route in (admin_router.upsert_product,
                  admin_router.retire_product,
                  admin_router.list_all_products):
        dependencies = [
            p.default.dependency
            for p in inspect.signature(route).parameters.values()
            if hasattr(p.default, "dependency")
        ]
        assert require_admin in dependencies, route.__name__
