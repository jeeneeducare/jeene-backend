"""A price the server promised, in a form the client cannot edit.

The app never sends an amount. It asks what a product costs, gets back a token, and hands
that token to the order endpoint — which reads the price out of the token *and*
independently recomputes it from the database before charging anything. The token exists
so the two halves of a purchase can be separated in time without the price becoming the
client's to choose in between.

Same construction as `app/assets.py`, with two additions the notes links do not need:

**It binds to a uid.** A token minted for one student is refused for another, so lifting
one off a rooted device buys nothing.

**It carries a nonce.** Two quotes for the same product, in the same second, by the same
student produce different tokens. Without it the token would be a pure function of its
inputs and therefore stable, and a stable token is a coupon.

The payload is not encrypted and does not need to be. Everything in it — product, price,
duration — is already public in `GET /billing/products`. What the signature buys is that
the server wrote it, not that nobody can read it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from dataclasses import dataclass

from app.billing.gateway import constant_time_equals
from app.config import settings

logger = logging.getLogger(__name__)

#: Ten minutes. Long enough to read three cards and pick one, short enough that a price
#: change does not leave many outstanding promises behind it. The order endpoint
#: recomputes anyway, so this is the second line of defence rather than the first.
DEFAULT_TTL_SECONDS = 600

_FALLBACK_SECRET = secrets.token_hex(32)
_warned = False


class QuoteInvalid(Exception):
    """A token that was not minted here, is not for this caller, or has expired.

    One exception for all three. Telling a caller *which* of those went wrong tells
    somebody probing the endpoint whether they have a real token, a stale one, or
    somebody else's — and none of that is theirs to learn.
    """


@dataclass(frozen=True)
class Quote:
    """What the server promised, once a token has been verified."""

    uid: str
    product_id: str
    amount_paise: int
    currency: str
    duration_days: int
    issued_at: int


def _secret() -> str:
    global _warned
    configured = settings.jeene_quote_secret
    if configured:
        return configured
    if not _warned:
        logger.warning(
            "JEENE_QUOTE_SECRET is not set; signing price quotes with a per-process "
            "secret. Quotes will stop verifying across a restart or between workers."
        )
        _warned = True
    return _FALLBACK_SECRET


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _digest(payload: bytes) -> str:
    return hmac.new(_secret().encode(), payload, hashlib.sha256).hexdigest()


def sign(
    *,
    uid: str,
    product_id: str,
    amount_paise: int,
    currency: str,
    duration_days: int,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Mint a token for one student, one product, one price.

    The amount is included so the order endpoint can compare what was promised against
    what the product costs now, and refuse a purchase whose price moved underneath it
    rather than charging either the old figure or the new one silently.
    """
    payload = json.dumps(
        {
            "uid": uid,
            "product_id": product_id,
            "amount_paise": amount_paise,
            "currency": currency,
            "duration_days": duration_days,
            "iat": int(time.time()),
            "exp": int(time.time()) + ttl_seconds,
            # Makes two otherwise identical quotes distinct. See the module docstring.
            "nonce": secrets.token_urlsafe(9),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return f"{_b64(payload)}.{_digest(payload)}"


def verify(token: str, *, uid: str) -> Quote:
    """Read a token back, or raise [QuoteInvalid].

    Fails closed on anything it does not understand. A malformed token and a forged one
    are the same event as far as the caller is concerned.
    """
    if not token:
        raise QuoteInvalid("no quote")

    encoded, _, digest = token.partition(".")
    if not digest:
        raise QuoteInvalid("malformed quote")

    try:
        payload = _unb64(encoded)
    except Exception as exc:  # noqa: BLE001 — any decoding failure is the same failure
        raise QuoteInvalid("malformed quote") from exc

    # Constant time: a comparison that returns early leaks the digest a byte at a time,
    # and a digest is all somebody needs to mint their own price.
    #
    # Encoded, because `compare_digest` raises on two strings holding anything outside
    # ASCII — and a quote is a client-supplied string, so one accented character in it
    # answered 500 instead of "that price is no longer valid".
    if not constant_time_equals(digest, _digest(payload)):
        raise QuoteInvalid("quote was not signed here")

    try:
        body = json.loads(payload)
    except ValueError as exc:
        raise QuoteInvalid("malformed quote") from exc

    if body.get("exp", 0) < int(time.time()):
        raise QuoteInvalid("quote expired")

    # Checked after the signature, deliberately. Verifying the binding first would let a
    # caller learn whether a token they do not own is otherwise valid.
    if body.get("uid") != uid:
        raise QuoteInvalid("quote belongs to somebody else")

    return Quote(
        uid=body["uid"],
        product_id=body["product_id"],
        amount_paise=int(body["amount_paise"]),
        currency=body["currency"],
        duration_days=int(body["duration_days"]),
        issued_at=int(body["iat"]),
    )
