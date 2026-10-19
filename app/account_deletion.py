"""Deleting a student's account, and everything it holds.

Both stores require it of any app that lets people make an account — Google Play wants a
way to delete the account and its data from inside the app, Apple's guideline 5.1.1(v)
the same — and it is the student's right in any case. So "delete my account" means
exactly that:

  * **Their data goes.** Answers, tests, plans, Ask Jeene conversations, Pro access: the
    `users` row cascades through every table that hangs off it.
  * **Their sign-in goes.** The Firebase account is deleted, so the same phone number or
    Google account starts from nothing next time rather than walking back into a husk.
  * **Their profile photo goes**, from Firebase Storage.

One thing is kept, because the law requires it and nothing else can stand in for it:
**purchase records.** A student who bought a pass leaves a `users` row with every
personal field emptied and `deleted_at` set, so the `payments` and `store_purchases`
rows that reference it stay explainable for tax and for refunds a store reports later.
Nothing in that row says who they were.

The order is deliberate. The database is cleared first, inside one transaction; the
Firebase account second. If Firebase cannot be reached the data is already gone, and the
app is told to try again — which is idempotent: a second call finds no row and goes
straight to the part that failed. The other order would leave data behind with no
account left to sign in and ask for it to be deleted.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import unquote, urlparse

import asyncpg

logger = logging.getLogger(__name__)

#: What a student's account holds, apart from the `users` row itself. Only needed when the
#: row is kept for its purchase records; otherwise deleting the row cascades through all
#: of these. Every one cascades on to its own children (steps, responses, messages).
_ACTIVITY_TABLES = (
    "attempts",
    "test_sessions",
    "plan_generations",
    "study_plans",
    "doubt_messages",
    "doubt_threads",
    "entitlement_grants",
)

DELETED = "deleted"
ANONYMISED = "anonymised"
ABSENT = "absent"


class IdentityDeletionFailed(Exception):
    """The data is gone, but the sign-in could not be deleted just now. Safe to retry."""


async def erase(connection: asyncpg.Connection, uid: str) -> tuple[str, str | None]:
    """Delete everything this backend holds about a student.

    Returns what happened — deleted outright, anonymised around purchase records, or
    nothing to delete — and the profile photo URL, so the caller can remove the file.
    """
    async with connection.transaction():
        row = await connection.fetchrow(
            "SELECT photo_url FROM users WHERE firebase_uid = $1 FOR UPDATE", uid
        )
        if row is None:
            return ABSENT, None
        photo_url = row["photo_url"]

        bought = await connection.fetchval(
            """
            SELECT EXISTS (SELECT 1 FROM payments WHERE firebase_uid = $1)
                OR EXISTS (SELECT 1 FROM store_purchases WHERE firebase_uid = $1)
            """,
            uid,
        )
        if not bought:
            await connection.execute("DELETE FROM users WHERE firebase_uid = $1", uid)
            logger.info("account deleted: %s", uid)
            return DELETED, photo_url

        for table in _ACTIVITY_TABLES:
            # The table name comes from the constant tuple above, never from a request.
            await connection.execute(f"DELETE FROM {table} WHERE firebase_uid = $1", uid)
        await connection.execute(
            """
            UPDATE users
               SET display_name = NULL, email = NULL, phone = NULL, photo_url = NULL,
                   class_level = NULL, target_exam = NULL, auth_provider = NULL,
                   pro_expires_at = NULL, last_login_at = NULL,
                   deleted_at = now(), updated_at = now()
             WHERE firebase_uid = $1
            """,
            uid,
        )
        logger.info("account deleted, purchase records kept: %s", uid)
        return ANONYMISED, photo_url


async def delete_identity(uid: str, photo_url: str | None) -> None:
    """Delete the Firebase account, then the profile photo if it is one we stored.

    A missing account is success: it is what a retry after a partial failure finds. The
    photo is best effort — a file that will not delete is logged, never a reason to tell a
    student their account was not deleted.
    """
    import firebase_admin
    from firebase_admin import auth

    if not firebase_admin._apps:
        # No Firebase credential: the test suite, or a laptop. Nothing to delete.
        logger.warning("Firebase is not initialised; skipping sign-in deletion for %s", uid)
        return

    try:
        await asyncio.to_thread(auth.delete_user, uid)
    except auth.UserNotFoundError:
        pass
    except Exception as exc:  # noqa: BLE001 — the network, Google, anything
        logger.error("could not delete the Firebase account of %s", uid)
        raise IdentityDeletionFailed("the sign-in could not be deleted") from exc

    location = _storage_object(photo_url)
    if location is None:
        return
    bucket_name, blob_name = location
    try:
        from firebase_admin import storage

        await asyncio.to_thread(lambda: storage.bucket(bucket_name).blob(blob_name).delete())
    except Exception:  # noqa: BLE001
        logger.warning("could not delete the profile photo of %s", uid)


def _storage_object(photo_url: str | None) -> tuple[str, str] | None:
    """The bucket and object behind a Firebase Storage download URL, or None.

    Only our own uploads are touched: `avatars/…` in a Firebase Storage bucket. A Google
    profile picture (`lh3.googleusercontent.com/…`) is Google's, not ours to delete.
    """
    if not photo_url:
        return None
    parsed = urlparse(photo_url)
    if parsed.hostname != "firebasestorage.googleapis.com":
        return None
    # /v0/b/<bucket>/o/<url-encoded object path>
    parts = parsed.path.split("/")
    if len(parts) < 6 or parts[1] != "v0" or parts[2] != "b" or parts[4] != "o":
        return None
    blob = unquote(parts[5])
    if not blob.startswith("avatars/"):
        return None
    return parts[3], blob
