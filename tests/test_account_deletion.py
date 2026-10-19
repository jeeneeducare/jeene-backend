"""Deleting an account: what goes, what is kept, and what a retry finds.

Both stores require an app that makes accounts to let people delete them, data and all.
Asserted here against a real database:

  * a student who never bought anything leaves nothing behind;
  * one who did leaves only an anonymised row holding their purchase records together —
    no name, no email, no phone, no answers, no Pro;
  * the sign-in is deleted after the data, and a failure there is a retry, not a loss;
  * only our own uploads are deleted from storage, never a Google profile picture.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import account_deletion, db

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="deleting an account writes to the database"
)

TENANT = "JEENE_MASTER"
STUDENT = "test-delete-student"
PHOTO = ("https://firebasestorage.googleapis.com/v0/b/jeene-ff8e3.appspot.com/o/"
         "avatars%2Ftest-delete-student.jpg?alt=media&token=abc")


def _sql(statement, *args):
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await db._init_connection(conn)
        try:
            return await conn.fetch(statement, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


def _one(statement, *args):
    rows = _sql(statement, *args)
    return rows[0][0] if rows else None


class Identity:
    """Firebase, faked at the one function that reaches it."""

    def __init__(self) -> None:
        self.deleted: list[tuple[str, str | None]] = []
        self.fails = False

    async def __call__(self, uid: str, photo_url: str | None) -> None:
        if self.fails:
            raise account_deletion.IdentityDeletionFailed("Firebase is down")
        self.deleted.append((uid, photo_url))


@pytest.fixture
def identity(monkeypatch):
    fake = Identity()
    monkeypatch.setattr(account_deletion, "delete_identity", fake)
    return fake


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


@pytest.fixture(autouse=True)
def a_student_with_history():
    """A student with answers, a conversation and some Pro, rebuilt for every test."""
    _clean()
    _sql("""INSERT INTO users (firebase_uid, tenant_id, display_name, email, phone,
                               class_level, target_exam, photo_url)
            VALUES ($1, $2, 'Asha', 'asha@example.com', '+919800000000', 12, 'neet', $3)""",
         STUDENT, TENANT, PHOTO)
    question = _one("SELECT question_id FROM questions LIMIT 1")
    chapter = _one("SELECT node_id FROM nodes WHERE type = 'chapter' LIMIT 1")
    _sql("""INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id,
                                  selected_option_ids, is_correct)
            VALUES ($1, $2, $3, $4, ARRAY['a'], true)""",
         uuid.uuid4(), STUDENT, TENANT, question)
    _sql("INSERT INTO doubt_threads (firebase_uid, tenant_id, chapter_id) VALUES ($1, $2, $3)",
         STUDENT, TENANT, chapter)
    _sql("""INSERT INTO entitlement_grants (firebase_uid, tenant_id, days, provider, note)
            VALUES ($1, $2, 30, 'manual', 'test')""", STUDENT, TENANT)
    yield
    _clean()


def _clean():
    _sql("DELETE FROM store_purchases WHERE firebase_uid = $1", STUDENT)
    _sql("DELETE FROM users WHERE firebase_uid = $1", STUDENT)


def _bought_a_pass():
    product = _one("SELECT product_id FROM products ORDER BY sort_order LIMIT 1")
    _sql("""INSERT INTO store_purchases (store, store_ref, store_product_id, product_id,
                                         firebase_uid, tenant_id, duration_days,
                                         environment, status)
            VALUES ('google', 'tok-deleted', 'pro_pass_30d', $1, $2, $3, 30,
                    'production', 'granted')""", product, STUDENT, TENANT)


def _activity():
    return {
        table: _one(f"SELECT count(*) FROM {table} WHERE firebase_uid = $1", STUDENT)
        for table in ("attempts", "doubt_threads", "entitlement_grants")
    }


def test_a_student_who_bought_nothing_leaves_nothing_behind(client, identity):
    response = client.delete("/auth/me")

    assert response.status_code == 204
    assert _one("SELECT count(*) FROM users WHERE firebase_uid = $1", STUDENT) == 0
    assert _activity() == {"attempts": 0, "doubt_threads": 0, "entitlement_grants": 0}
    assert identity.deleted == [(STUDENT, PHOTO)], "the sign-in and the photo go too"


def test_purchase_records_outlive_the_account_with_nobody_in_them(client, identity):
    _bought_a_pass()

    response = client.delete("/auth/me")

    assert response.status_code == 204
    row = _sql("SELECT * FROM users WHERE firebase_uid = $1", STUDENT)[0]
    assert row["deleted_at"] is not None
    for field in ("display_name", "email", "phone", "photo_url", "class_level",
                  "target_exam", "pro_expires_at"):
        assert row[field] is None, field
    assert _activity() == {"attempts": 0, "doubt_threads": 0, "entitlement_grants": 0}
    assert _one("SELECT count(*) FROM store_purchases WHERE firebase_uid = $1", STUDENT) == 1


def test_the_data_goes_first_and_a_failed_sign_in_deletion_is_a_retry(client, identity):
    identity.fails = True
    failed = client.delete("/auth/me")

    assert failed.status_code == 502
    assert "Try again" in failed.json()["detail"]
    assert _one("SELECT count(*) FROM users WHERE firebase_uid = $1", STUDENT) == 0, \
        "the data was already gone before Firebase was asked"

    identity.fails = False
    retried = client.delete("/auth/me")

    assert retried.status_code == 204
    assert identity.deleted == [(STUDENT, None)], "nothing left to erase; only the sign-in"


def test_signing_in_again_after_a_half_finished_deletion_is_a_fresh_start(client, identity):
    _bought_a_pass()
    identity.fails = True
    client.delete("/auth/me")

    identity.fails = False
    client.post("/auth/session", json={})

    assert _one("SELECT deleted_at FROM users WHERE firebase_uid = $1", STUDENT) is None
    assert _activity()["attempts"] == 0


@pytest.mark.parametrize("url, expected", [
    (PHOTO, ("jeene-ff8e3.appspot.com", "avatars/test-delete-student.jpg")),
    # A Google account's own picture is Google's to keep.
    ("https://lh3.googleusercontent.com/a/abc=s96-c", None),
    # Our bucket, but not an upload of ours.
    ("https://firebasestorage.googleapis.com/v0/b/jeene-ff8e3.appspot.com/o/notes%2Fch1.pdf", None),
    (None, None),
    ("", None),
])
def test_only_our_own_uploads_are_deleted_from_storage(url, expected):
    assert account_deletion._storage_object(url) == expected
