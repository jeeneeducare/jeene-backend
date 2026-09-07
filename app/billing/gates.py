"""What is free, what Pro unlocks, and what a refusal looks like.

Every number a student can feel lives in the block at the top of this file. Moving one is
a one-line change here rather than a search through five routers, which is the whole
reason this module exists.

Three rules shape everything below.

**The server refuses; the apps only dim.** An app that hides a button is a courtesy. The
gate is here, on the write path, because a hidden button is one HTTP client away from not
being hidden.

**A refusal explains itself.** A gate answers `402` with a reason, the copy to show, and
the counters behind it. The wording is chosen here so both platforms say the same thing,
and so it names what was blocked — "Mock papers are part of Pro" reads as an explanation,
"Upgrade now" reads as an advertisement.

**Nothing is locked on a deployment that cannot sell Pro.** With `JEENE_BILLING_ENABLED`
off, every gate passes. A student must never meet a paywall that has no way through it,
so the switch that turns on selling is the same switch that turns on locking.

There is deliberately no `require_pro` dependency. Two of the four gates have to run
*after* a lookup — a sitting already in progress is resumable even if Pro lapsed
mid-paper, and a plan being resumed is not a new plan — and a dependency that runs before
the handler cannot know either. So the guard is a call, [ensure_pro], placed where the
answer is actually known.
"""

from __future__ import annotations

import asyncpg
from fastapi import HTTPException

from app.billing import entitlements
from app.config import settings
from app.schemas import GateBlocked

# --- the free tier, in one place ---------------------------------------------------------

#: Answers a day, free. Enough to prove the question bank is real; a rolling window rather
#: than a calendar day, so a student in any timezone gets the same allowance.
FREE_QUESTIONS_PER_DAY = 20

#: Jeene Mode plans, free, ever. One is the strongest demonstration the product has and it
#: costs a single model call. Resuming a plan already made is not a new one.
FREE_PLANS_EVER = 1

#: Notes and video lectures are free for the first chapter of each subject — enough to see
#: what the material is like before paying for the rest.
FREE_CHAPTERS_PER_SUBJECT = 1

# --- what a client is told ---------------------------------------------------------------

PRO_ONLY = "pro_only"
DAILY_PRACTICE_LIMIT = "daily_practice_limit"
PLAN_LIMIT = "plan_limit"
CHAPTER_LOCKED = "chapter_locked"


def enforced() -> bool:
    """Whether gates bite on this deployment.

    Off until billing is on. A locked chapter with no way to unlock it is worse than an
    unlocked one, and this is what keeps the two from ever getting out of step.
    """
    return bool(settings.jeene_billing_enabled)


def blocked(reason: str, message: str, *, used: int | None = None,
            limit: int | None = None) -> HTTPException:
    """A `402` carrying enough for the app to draw the right sheet without guessing."""
    return HTTPException(
        status_code=402,
        detail=GateBlocked(
            reason=reason, message=message, tier=entitlements.PRO, used=used, limit=limit
        ).model_dump(),
    )


# --- who has what ------------------------------------------------------------------------


async def is_pro(connection: asyncpg.Connection, uid: str, tenant: str) -> bool:
    """Whether this student's access is live right now.

    One indexed read of the cache every grant writes. The date is compared here and never
    on the device: a phone with a wrong clock must not be able to unlock anything.
    """
    held = await entitlements.entitlement_of(connection, uid, tenant)
    return held.active


async def ensure_pro(
    connection: asyncpg.Connection, uid: str, tenant: str, *, message: str
) -> None:
    """Let a Pro student through, or refuse with copy that says what was blocked."""
    if not enforced() or await is_pro(connection, uid, tenant):
        return
    raise blocked(PRO_ONLY, message)


# --- practice ----------------------------------------------------------------------------


async def ensure_can_answer(
    connection: asyncpg.Connection, uid: str, tenant: str, *, attempt_id: str
) -> None:
    """The daily practice allowance, counted from `attempts` rather than a counter table.

    Counting the rows is not a shortcut — it is the only version that survives a retry.
    Attempt ids are minted by the client so a resent request cannot double-count an
    answer, and a separate counter would be incremented by the retry that the insert then
    ignores.

    For the same reason this attempt is excluded from its own count: a request that has
    already been recorded and is being retried must not be refused because it is itself
    the twenty-first row.
    """
    if not enforced() or await is_pro(connection, uid, tenant):
        return

    used = await connection.fetchval(
        """
        SELECT count(*) FROM attempts
         WHERE firebase_uid = $1
           AND created_at > now() - interval '1 day'
           AND attempt_id <> $2::uuid
        """,
        uid, attempt_id,
    )
    if used >= FREE_QUESTIONS_PER_DAY:
        raise blocked(
            DAILY_PRACTICE_LIMIT,
            f"You have answered your {FREE_QUESTIONS_PER_DAY} free questions for today. "
            "Pro removes the daily limit.",
            used=used, limit=FREE_QUESTIONS_PER_DAY,
        )


# --- Jeene Mode --------------------------------------------------------------------------


async def ensure_can_plan(connection: asyncpg.Connection, uid: str, tenant: str) -> None:
    """The lifetime free-plan allowance.

    Counted from `study_plans`, so it is plans a student actually received. A generation
    that failed produced nothing and must not be charged against them; archiving one does
    not give the allowance back, because they did get it.

    Call this only where a plan is about to be *made*. Resuming the plan for a scope
    already planned is not a new plan, and telling somebody their own saved work is behind
    a paywall is the worst version of this screen.
    """
    if not enforced() or await is_pro(connection, uid, tenant):
        return

    made = await connection.fetchval(
        "SELECT count(*) FROM study_plans WHERE firebase_uid = $1", uid
    )
    if made >= FREE_PLANS_EVER:
        raise blocked(
            PLAN_LIMIT,
            "Free accounts get one Jeene Mode plan. Pro plans as many chapters as you "
            "like, and rebuilds them as you improve.",
            used=made, limit=FREE_PLANS_EVER,
        )


# --- notes and lectures ------------------------------------------------------------------

#: The first chapter of each subject, by the same ordering `/chapters` lists them in.
#: Computed rather than flagged, because `nodes` belongs to the pipeline and this backend
#: does not write content tables. Kept in one statement so the day it becomes a table an
#: admin edits, nothing above it changes.
_FREE_CHAPTER_SQL = """
WITH RECURSIVE up AS (
    SELECT node_id, parent_id, type
      FROM nodes WHERE node_id = $1 AND tenant_id = $2
    UNION ALL
    SELECT n.node_id, n.parent_id, n.type
      FROM nodes n JOIN up ON up.parent_id = n.node_id
     WHERE n.tenant_id = $2
),
chapter AS (SELECT node_id FROM up WHERE type = 'chapter' LIMIT 1),
free AS (
    SELECT DISTINCT ON (subject_id) node_id
      FROM nodes
     WHERE tenant_id = $2 AND type = 'chapter' AND status = 'published'
       AND subject_id IS NOT NULL
     ORDER BY subject_id, class_level, ncert_chapter_number NULLS LAST, title
)
SELECT EXISTS (SELECT 1 FROM chapter c JOIN free f ON f.node_id = c.node_id)
"""


async def is_free_chapter(
    connection: asyncpg.Connection, node_id: str, tenant: str
) -> bool:
    """Whether this node sits in a chapter that is free to everybody.

    Takes any node, not just a chapter: the video screens are opened from topics and
    concepts, and what decides the lock is the chapter they hang under. A node with no
    chapter above it is treated as locked — the safe direction for a paywall, and not a
    shape any of these routes is called with.
    """
    return bool(await connection.fetchval(_FREE_CHAPTER_SQL, node_id, tenant))


async def ensure_chapter_open(
    connection: asyncpg.Connection,
    node_id: str,
    tenant: str,
    user: dict | None,
    *,
    what: str,
) -> None:
    """Let anything under a free chapter through; everything else needs Pro.

    `user` may be None. These routes are readable signed out — a student deciding whether
    to make an account should be able to look at the first chapter — and an anonymous
    caller is simply on the free tier.
    """
    if not enforced():
        return
    if await is_free_chapter(connection, node_id, tenant):
        return
    if user is not None and await is_pro(connection, user["uid"], tenant):
        return

    raise blocked(
        CHAPTER_LOCKED,
        f"{what} for this chapter are part of Pro. The first chapter of each subject is "
        "free to read and watch.",
    )
