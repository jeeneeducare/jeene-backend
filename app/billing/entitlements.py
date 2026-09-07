"""Who has Pro, and until when.

The ledger is the truth. `entitlement_grants` is append-only, and access is derived by
folding those grants in order — never a mutable expiry column that some code path edits.
That choice is what makes the four confirmation paths in the payments flow safe: a
webhook delivered twice, a reconciler racing a client verify, a retry after a timeout.
Each tries to insert a grant, the unique index on `order_id` refuses the second, and the
answer is the same either way.

`users.pro_expires_at` is a cache of exactly that fold, written in the same transaction
as the grant. It exists so the gate on a practice request is one indexed read rather than
a scan over a growing table. Nothing may treat it as authoritative: [recompute] is the
definition, and [entitlement_of] reads the cache only because the two are kept in step by
construction.

Two behaviours follow from folding rather than summing, and both are what a student would
call fair. **Stacking**: three months bought while a month is still running gives four,
not three. **Lapsing**: somebody who expires in January and returns in February gets
thirty days from February, not thirty days from January's expiry — they are not owed the
weeks they spent not subscribed, and they must not be charged for them either.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import asyncpg

#: The only tier that exists today. Named rather than inlined so the day a second one
#: arrives is a day of adding rows, not of finding every literal 'pro' in the codebase.
PRO = "pro"


@dataclass(frozen=True)
class Entitlement:
    """What a student is entitled to, right now."""

    tier: str
    #: None when they have never held this tier.
    expires_at: datetime | None

    @property
    def active(self) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at > datetime.now(timezone.utc)

    @property
    def days_remaining(self) -> int:
        """Whole days left, floored at zero. For the app's "expires in 12 days"."""
        if not self.active or self.expires_at is None:
            return 0
        return max(0, (self.expires_at - datetime.now(timezone.utc)).days)

    @classmethod
    def none(cls, tier: str = PRO) -> "Entitlement":
        return cls(tier=tier, expires_at=None)


async def recompute(
    connection: asyncpg.Connection, uid: str, tenant: str, tier: str = PRO
) -> datetime | None:
    """The expiry this student's grants add up to, or None if they have none.

    Folded in order rather than summed, because summing is wrong the moment somebody
    lapses. A student who bought thirty days on 1 January and thirty more on 15 February
    is owed until 17 March — not until 2 March, which is what "sixty days from the first
    grant" would say. The days they were not a subscriber are not days they paid for.

    Each grant therefore starts from whichever is later: the moment it was granted, or
    the expiry it is extending. Stacking and lapsing both fall out of that one rule, and
    a refund (negative `days`) simply pulls the running expiry backwards.
    """
    rows = await connection.fetch(
        """
        SELECT days, granted_at
          FROM entitlement_grants
         WHERE firebase_uid = $1 AND tenant_id = $2 AND tier = $3
         ORDER BY granted_at, grant_id
        """,
        uid, tenant, tier,
    )
    if not rows:
        return None

    expiry: datetime | None = None
    for row in rows:
        start = row["granted_at"] if expiry is None else max(row["granted_at"], expiry)
        expiry = start + timedelta(days=row["days"])
    return expiry


async def refresh_cache(
    connection: asyncpg.Connection, uid: str, tenant: str, tier: str = PRO
) -> datetime | None:
    """Recompute and write `users.pro_expires_at`. Call inside the granting transaction.

    Returns the value written, so a caller that has just granted access can answer the
    student without a second round trip.
    """
    expires_at = await recompute(connection, uid, tenant, tier)
    await connection.execute(
        "UPDATE users SET pro_expires_at = $2, updated_at = now() WHERE firebase_uid = $1",
        uid, expires_at,
    )
    return expires_at


async def entitlement_of(
    connection: asyncpg.Connection, uid: str, tenant: str, tier: str = PRO
) -> Entitlement:
    """What to tell the app. Reads the cache; falls back to the ledger if it is unset.

    The fallback is not paranoia about the cache drifting — it cannot, being written in
    the same transaction as every grant. It is for the rows that predate this column,
    which have a null there and a perfectly good ledger underneath.
    """
    cached = await connection.fetchval(
        "SELECT pro_expires_at FROM users WHERE firebase_uid = $1", uid
    )
    if cached is not None:
        return Entitlement(tier=tier, expires_at=cached)

    computed = await recompute(connection, uid, tenant, tier)
    if computed is not None:
        # Fill it in, so this path is taken once per user at most.
        await connection.execute(
            "UPDATE users SET pro_expires_at = $2 WHERE firebase_uid = $1", uid, computed
        )
    return Entitlement(tier=tier, expires_at=computed)


async def grant(
    connection: asyncpg.Connection,
    *,
    uid: str,
    tenant: str,
    days: int,
    provider: str,
    order_id: str | None = None,
    tier: str = PRO,
    note: str = "",
) -> bool:
    """Add days to a student's access. Returns whether this call was the one that did it.

    Idempotent on `order_id`: the unique index refuses a second grant for the same
    payment, and `ON CONFLICT DO NOTHING` turns that refusal into a False rather than an
    exception. Two confirmation paths racing therefore produce one grant, decided by the
    database rather than by whichever coroutine happened to be scheduled first.

    Call inside a transaction with the payment row locked. The cache refresh below is
    part of that same transaction, so there is no window in which the ledger and the
    cache disagree.
    """
    inserted = await connection.fetchval(
        """
        INSERT INTO entitlement_grants
               (firebase_uid, tenant_id, tier, days, provider, order_id, note)
        VALUES ($1, $2, $3, $4, $5, $6, $7)
        ON CONFLICT DO NOTHING
        RETURNING grant_id
        """,
        uid, tenant, tier, days, provider, order_id, note,
    )
    if inserted is None:
        return False
    await refresh_cache(connection, uid, tenant, tier)
    return True
