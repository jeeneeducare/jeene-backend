"""Short-lived links to files we host but do not hand out.

A chapter's notes live in object storage, and the URL of that object is durable,
unauthenticated and shareable. Handing it to the app means handing it to everyone the
student ever forwards it to — which is why `chapter_notes.pdf_url` has carried a note
since the table was written saying the reader would eventually be served through our own
viewer rather than given the file.

This is the piece that makes that possible: a signed, expiring reference to something,
which the app can put in a web view and which is worthless tomorrow. It is not an access
control system. Anyone who can ask for the notes can get a link, exactly as before — what
changes is that the link they get stops working, and the storage URL behind it never
leaves this process.

Deliberately no I/O and no database: signing is a pure function of the secret, so it can
be tested without either, and the two places that call it cannot disagree about the
format.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time

from app.config import settings
from app.security import constant_time_equals

logger = logging.getLogger(__name__)

#: How long a minted link stays good. Long enough to open the viewer and read for a
#: while, short enough that a forwarded link is a dead link by the time it arrives.
DEFAULT_TTL_SECONDS = 30 * 60

#: Used when nothing is configured. A restart invalidates every outstanding link, which
#: is fine for one process on a laptop and wrong for more than one process anywhere else
#: — set `JEENE_ASSET_SECRET` in any deployment that runs more than a single worker.
_FALLBACK_SECRET = secrets.token_hex(32)
_warned = False


def _secret() -> str:
    global _warned
    configured = settings.jeene_asset_secret
    if configured:
        return configured
    if not _warned:
        logger.warning(
            "JEENE_ASSET_SECRET is not set; signing asset links with a per-process "
            "secret. Links will stop working across a restart or between workers."
        )
        _warned = True
    return _FALLBACK_SECRET


def _digest(kind: str, ref: str, expires_at: int) -> str:
    return hmac.new(
        _secret().encode(),
        f"{kind}:{ref}:{expires_at}".encode(),
        hashlib.sha256,
    ).hexdigest()


def sign(kind: str, ref: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """A token good for this one thing, for a while.

    `kind` keeps a token minted for one sort of asset from opening another: without it a
    link to a chapter's notes would also be a valid link to anything else keyed by the
    same id.
    """
    expires_at = int(time.time()) + ttl_seconds
    return f"{expires_at}.{_digest(kind, ref, expires_at)}"


def verify(kind: str, ref: str, token: str) -> bool:
    """Whether this token was minted here, for this thing, and has not expired.

    Fails closed on anything it does not understand — a malformed token is not an error
    worth distinguishing from a wrong one, and saying which it was tells an attacker
    something.
    """
    if not token:
        return False
    head, _, digest = token.partition(".")
    if not digest:
        return False
    try:
        expires_at = int(head)
    except ValueError:
        return False
    if expires_at < int(time.time()):
        return False
    # Constant time: a comparison that returns early leaks the digest a byte at a time.
    return constant_time_equals(digest, _digest(kind, ref, expires_at))
