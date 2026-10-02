"""Fetching a document we did not write the URL of.

`chapter_notes.pdf_url` is written by the content pipeline, not through this API, so it is
not attacker-controlled today. That is a fact about a different repository rather than a
property of this code, and it is not a thing to build on — so everything that reaches out
to it goes through the check below.

This lived inside the notes router until a second caller appeared: Ask Jeene needs the
same bytes, for text rather than for streaming, and a security check with two copies is a
security check with one out-of-date copy.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app.config import settings

logger = logging.getLogger(__name__)

#: One message for every refusal. Which check failed is in the log, not in the response:
#: telling a caller *why* a URL was rejected is telling them how to pick the next one.
STORAGE_REFUSED = "That document is not available"


STORAGE_TIMEOUT = httpx.Timeout(10.0, read=30.0)

#: The most we will pass through. Chapter notes are a few megabytes; anything an order of
#: magnitude past that is either a mistake or somebody using this as a relay.
MAX_BYTES = 64 * 1024 * 1024


def _allowed_hosts() -> set[str]:
    raw = settings.jeene_notes_storage_hosts or ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def check_storage_url(url: str) -> None:
    """Refuse to fetch anything that is not a public document.

    This function is the difference between a file proxy and an open relay into our own
    network. Verified before it existed: with `pdf_url` set to `http://127.0.0.1:8000/health`
    this endpoint fetched it and handed the body back to the caller. On a cloud host the
    same shape reaches the instance metadata service, and the response goes to whoever
    asked.

    `pdf_url` is written by the content pipeline rather than through this API, so it is
    not attacker-controlled today. That is a fact about a different repository, not a
    property of this code, and it is not a thing to rely on.

    Two modes. With `JEENE_NOTES_STORAGE_HOSTS` set, only those hosts are allowed and the
    scheme is not policed — an operator naming a host has said what they mean, and it is
    how a local setup points at a plain-http fixture. With it unset, the rule is: https,
    and an address that is globally routable.

    Known limit: the name is resolved here and again by the client, so a host that
    answers differently between the two could still slip through. Closing that means
    connecting to the checked address with the Host header set, which is more machinery
    than this is worth today — but redirects are off, which removes the easy version of
    the same trick.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=502, detail=STORAGE_REFUSED)

    allowed = _allowed_hosts()
    if allowed:
        if host not in allowed:
            logger.error("Notes storage host %r is not in the allow-list", host)
            raise HTTPException(status_code=502, detail=STORAGE_REFUSED)
        return

    if parsed.scheme != "https":
        logger.error("Notes storage URL is not https: %r", parsed.scheme)
        raise HTTPException(status_code=502, detail=STORAGE_REFUSED)

    try:
        resolved = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise HTTPException(status_code=502, detail=STORAGE_REFUSED) from None
    for info in resolved:
        address = ipaddress.ip_address(info[4][0])
        # `is_global` is false for loopback, link-local (including 169.254.169.254),
        # private ranges, and the unspecified address — every target worth having.
        if not address.is_global:
            logger.error("Notes storage host %r resolves to %s", host, address)
            raise HTTPException(status_code=502, detail=STORAGE_REFUSED)


#: Streaming chunk. Large enough not to thrash, small enough that a slow reader does not
#: hold a big buffer per connection.
_CHUNK = 64 * 1024


async def stream_document(url: str, *, what: str, cache_control: str) -> StreamingResponse:
    """A PDF from storage, relayed through this process so the caller never learns where
    it lives.

    The notes route grew this logic first and is tested line by line where it stands, so
    it keeps its own copy; the NCERT reader is the second caller and the first to use this
    one. The rules are the notes route's, each paid for once already:

      * the URL is checked before anything is fetched (`check_storage_url`);
      * redirects are off, because a 302 from an allowed host is how an allowed URL turns
        into a fetch of something else;
      * nothing past `MAX_BYTES` is relayed, and nothing past the length upstream declared
        either, so a response can never promise more bytes than it sends.
    """
    check_storage_url(url)

    client = httpx.AsyncClient(timeout=STORAGE_TIMEOUT, follow_redirects=False)
    try:
        upstream = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=STORAGE_REFUSED) from exc
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        logger.error("Storage answered %s for %s", upstream.status_code, what)
        raise HTTPException(status_code=502, detail=STORAGE_REFUSED)

    declared = upstream.headers.get("content-length")
    promised = int(declared) if declared and declared.isdigit() else None
    if promised is not None and promised > MAX_BYTES:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=STORAGE_REFUSED)
    limit = promised if promised is not None else MAX_BYTES

    async def body():
        sent = 0
        try:
            async for chunk in upstream.aiter_bytes(_CHUNK):
                if sent + len(chunk) > limit:
                    logger.error("%s sent more than %d bytes; stopping", what, limit)
                    yield chunk[: limit - sent]
                    return
                sent += len(chunk)
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {"Cache-Control": cache_control}
    if promised is not None:
        headers["Content-Length"] = str(promised)
    return StreamingResponse(body(), media_type="application/pdf", headers=headers)
