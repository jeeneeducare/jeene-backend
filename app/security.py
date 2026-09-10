"""Comparisons that must not leak, and must not raise.

One function, in its own module, because it is needed by three unrelated parts of the
application — the notes viewer's signed links, a browser sitting a test, and every
signature in billing — and the last time it lived in one of them the other two grew their
own version with a bug in it.
"""

from __future__ import annotations

import hmac


def constant_time_equals(a: str, b: str) -> bool:
    """Whether two secrets match, in constant time, without ever raising.

    Two properties, and both were learned the hard way.

    **Constant time.** A comparison that returns on the first differing byte leaks the
    expected value a byte at a time, and the expected value here is variously a forged
    success callback, somebody else's exam, or a link to a document.

    **Encoded first.** `hmac.compare_digest` refuses two *strings* containing anything
    outside ASCII and raises `TypeError` — so one accented character in a token turned a
    404 or a 400 into a 500 on every route that checks one. That is a crash an anonymous
    caller can cause at will, on the payment webhook, on a notes link, and in the middle
    of a timed paper.

    UTF-8 with `surrogatepass`, so a lone surrogate from a mangled header is compared
    rather than raising on the way in.
    """
    return hmac.compare_digest(
        a.encode("utf-8", "surrogatepass"), b.encode("utf-8", "surrogatepass")
    )
