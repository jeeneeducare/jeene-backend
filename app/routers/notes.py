"""A chapter's written notes, and the viewer the app reads them in.

Three routes, and the split between them is the point:

  * `/chapters/{id}/notes` says what the notes are — title, length — and hands back a
    link to open them. It never returns the storage URL.
  * `/notes/{id}` is the reader: a page served from our own origin, which the apps load
    in the same web view they already use for the video player.
  * `/notes/{id}/file` streams the bytes through this process.

The storage URL is durable, unauthenticated and shareable; a link minted here is none of
those. `chapter_notes.pdf_url` has carried a comment since the table was written saying
the reader would be served through our own viewer rather than given the file, and this is
that. What it is not is access control — anyone who could ask for the notes before can
still ask now. What changes is that what they get stops working.

The cost is real and worth naming: the PDF goes through the API rather than straight from
storage to the device. Notes exist for eight chapters, so this is a few megabytes on the
rare occasion somebody opens one, and it is the price of not handing out the object.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from pathlib import Path
from urllib.parse import urlparse

import asyncpg
import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from app import assets
from app.config import settings
from app.auth import current_tenant
from app.db import get_connection
from app.schemas import ChapterNotes

logger = logging.getLogger(__name__)

router = APIRouter()

#: What a signed notes link is *for*, so a token minted for these notes cannot be
#: replayed against some other asset that happens to share the id.
ASSET_KIND = "notes"

#: Streaming chunk. Large enough not to thrash, small enough that a slow reader does not
#: hold a big buffer per connection.
_CHUNK = 64 * 1024

#: How long we will wait on storage. A read that has stalled this long is not coming.
_STORAGE_TIMEOUT = httpx.Timeout(10.0, read=30.0)

#: The most we will pass through. Chapter notes are a few megabytes; anything an order of
#: magnitude past that is either a mistake or somebody using this as a relay.
_MAX_BYTES = 64 * 1024 * 1024


def _allowed_hosts() -> set[str]:
    raw = settings.jeene_notes_storage_hosts or ""
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


def _check_storage_url(url: str) -> None:
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
        raise HTTPException(status_code=502, detail=_STORAGE_REFUSED)

    allowed = _allowed_hosts()
    if allowed:
        if host not in allowed:
            logger.error("Notes storage host %r is not in the allow-list", host)
            raise HTTPException(status_code=502, detail=_STORAGE_REFUSED)
        return

    if parsed.scheme != "https":
        logger.error("Notes storage URL is not https: %r", parsed.scheme)
        raise HTTPException(status_code=502, detail=_STORAGE_REFUSED)

    try:
        resolved = socket.getaddrinfo(host, parsed.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        raise HTTPException(status_code=502, detail=_STORAGE_REFUSED) from None
    for info in resolved:
        address = ipaddress.ip_address(info[4][0])
        # `is_global` is false for loopback, link-local (including 169.254.169.254),
        # private ranges, and the unspecified address — every target worth having.
        if not address.is_global:
            logger.error("Notes storage host %r resolves to %s", host, address)
            raise HTTPException(status_code=502, detail=_STORAGE_REFUSED)


#: One message for every refusal. Which check failed is in the log, not in the response:
#: telling a caller *why* their fetch was refused is telling them how to shape the next one.
_STORAGE_REFUSED = "The notes could not be read from storage"


async def _notes_row(
    connection: asyncpg.Connection, chapter_id: str, tenant: str
) -> asyncpg.Record:
    row = await connection.fetchrow(
        """
        SELECT n.chapter_id, n.title, n.pdf_url, n.page_count, n.size_bytes
          FROM chapter_notes n
          JOIN nodes c ON c.node_id = n.chapter_id
         WHERE n.chapter_id = $1 AND n.tenant_id = $2 AND n.status = 'published'
           AND c.status = 'published'
        """,
        chapter_id, tenant,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No notes for this chapter yet")
    return row


@router.get("/chapters/{chapter_id}/notes", response_model=ChapterNotes)
async def chapter_notes(
    chapter_id: str,
    request: Request,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> ChapterNotes:
    """The written notes for a chapter, if there are any published.

    404 when a chapter has none, which is most of them today: notes exist for eight
    physics chapters and nowhere else yet. The app asks when a chapter opens so the Notes
    tile can say whether there is anything behind it, rather than letting a student tap
    and find out.

    `page_count` and `size_bytes` are nullable because the columns are. They were once
    declared required here, which made a published row whose size had never been recorded
    answer 500 — for the only such row that existed.
    """
    row = await _notes_row(connection, chapter_id, tenant)
    origin = str(request.base_url).rstrip("/")
    token = assets.sign(ASSET_KIND, chapter_id)
    return ChapterNotes(
        chapter_id=row["chapter_id"],
        title=row["title"],
        page_count=row["page_count"],
        size_bytes=row["size_bytes"],
        viewer_url=f"{origin}/notes/{chapter_id}?t={token}",
    )


# The reader. Served from here rather than bundled in the apps for the same reason the
# video player is: it can be fixed without a release, and both platforms get the same one.
# That matters more here than it looks — iOS renders a PDF in a web view by itself and
# Android does not, so without a viewer of our own this would have been two
# implementations, one of them a native page renderer.
#
# PDF.js is served from here too, not from a CDN. The file already streams through this
# process, so a second host could only reduce the chance the page works. See
# `app/static/pdfjs/NOTICE.md`.
_VIEWER_PAGE = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{title}</title>
    <style>
      html, body {{ margin: 0; padding: 0; background: #080511; color: #B4ACD0;
                    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
                          Roboto, sans-serif; }}
      #pages {{ padding: 12px 10px 40px; }}
      canvas {{ display: block; width: 100%; height: auto; margin: 0 auto 12px;
                border-radius: 10px; background: #fff; }}
      #note {{ padding: 40px 24px; text-align: center; }}
    </style>
  </head>
  <body>
    <div id="pages"></div>
    <div id="note">Opening the notes…</div>
    <script src="{origin}/notes-viewer/pdf.min.js"></script>
    <script>
      // Not `status`. A global `var status` is `window.status`, which is a string: the
      // element is coerced on assignment, every write to `.textContent` is silently
      // dropped, and a page that has failed sits on its loading text forever. That is
      // exactly what happened the first time this was run.
      var note = document.getElementById("note");
      var pages = document.getElementById("pages");

      function fail(message) {{
        pages.innerHTML = "";
        note.textContent = message;
      }}

      if (typeof pdfjsLib === "undefined") {{
        fail("These notes could not be opened. Try again in a moment.");
      }} else {{
        pdfjsLib.GlobalWorkerOptions.workerSrc = "{origin}/notes-viewer/pdf.worker.min.js";

        // Rendered one page at a time and appended as each finishes, so a long document
        // starts showing itself immediately instead of after the whole thing is ready.
        function renderFrom(pdf, number) {{
          if (number > pdf.numPages) return;
          pdf.getPage(number).then(function (page) {{
            var width = pages.clientWidth || document.body.clientWidth;
            var unscaled = page.getViewport({{ scale: 1 }});
            // Cap the pixel ratio: at 3x a big page is a canvas some devices refuse.
            var ratio = Math.min(window.devicePixelRatio || 1, 2);
            var viewport = page.getViewport({{ scale: (width / unscaled.width) * ratio }});
            var canvas = document.createElement("canvas");
            canvas.width = viewport.width;
            canvas.height = viewport.height;
            pages.appendChild(canvas);
            page.render({{
              canvasContext: canvas.getContext("2d"), viewport: viewport
            }}).promise.then(function () {{
              note.textContent = "";
              renderFrom(pdf, number + 1);
            }});
          }}).catch(function () {{
            fail("These notes stopped partway through. Try opening them again.");
          }});
        }}

        pdfjsLib.getDocument("{file_url}").promise.then(function (pdf) {{
          renderFrom(pdf, 1);
        }}).catch(function () {{
          fail("These notes are not available just now.");
        }});
      }}
    </script>
  </body>
</html>
"""


#: Where the vendored renderer lives on disk.
_VIEWER_ASSETS = Path(__file__).resolve().parent.parent / "static" / "pdfjs"


@router.get("/notes-viewer/{filename}")
async def viewer_asset(filename: str) -> FileResponse:
    """PDF.js, served from our own origin.

    Not signed and not per-student: this is a public library, and the thing worth
    protecting is the document, not the renderer. Its own path rather than under
    `/notes/...` so it cannot be confused with a chapter id.
    """
    if filename not in ("pdf.min.js", "pdf.worker.min.js"):
        raise HTTPException(status_code=404, detail="No such viewer asset")
    return FileResponse(
        _VIEWER_ASSETS / filename,
        media_type="text/javascript",
        # Pinned to a version we ship, so it can be cached for as long as the browser
        # likes; a new version arrives at a new deployment, not at a new URL.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/notes/{chapter_id}", response_class=HTMLResponse)
async def notes_viewer(
    chapter_id: str,
    request: Request,
    t: str = Query(default=""),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> HTMLResponse:
    """The reader page, for the app to load in a web view."""
    if not assets.verify(ASSET_KIND, chapter_id, t):
        raise HTTPException(status_code=403, detail="This link has expired")
    row = await _notes_row(connection, chapter_id, tenant)
    origin = str(request.base_url).rstrip("/")
    return HTMLResponse(
        _VIEWER_PAGE.format(
            # The title is written into the page. It comes from the content pipeline
            # rather than from a request, but it is still not this template's to trust.
            title=_escape(row["title"]),
            origin=origin,
            file_url=f"{origin}/notes/{chapter_id}/file?t={t}",
        )
    )


@router.get("/notes/{chapter_id}/file")
async def notes_file(
    chapter_id: str,
    t: str = Query(default=""),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> StreamingResponse:
    """The PDF itself, streamed through this process.

    The whole reason the route exists: the client asks us, we ask storage, and the object
    URL stays here. Streamed rather than buffered so a large document does not sit in
    memory on the way past.
    """
    if not assets.verify(ASSET_KIND, chapter_id, t):
        raise HTTPException(status_code=403, detail="This link has expired")
    row = await _notes_row(connection, chapter_id, tenant)

    _check_storage_url(row["pdf_url"])

    # Redirects are off. A 302 from a legitimate host is how an allow-listed URL becomes
    # a fetch of something else entirely, and following one would undo the check above.
    client = httpx.AsyncClient(timeout=_STORAGE_TIMEOUT, follow_redirects=False)
    try:
        upstream = await client.send(
            client.build_request("GET", row["pdf_url"]), stream=True
        )
    except httpx.HTTPError as exc:
        await client.aclose()
        raise HTTPException(status_code=502, detail=_STORAGE_REFUSED) from exc
    if upstream.status_code != 200:
        await upstream.aclose()
        await client.aclose()
        logger.error(
            "Notes storage answered %s for %s", upstream.status_code, chapter_id
        )
        raise HTTPException(status_code=502, detail=_STORAGE_REFUSED)

    declared = upstream.headers.get("content-length")
    promised = int(declared) if declared and declared.isdigit() else None
    if promised is not None and promised > _MAX_BYTES:
        await upstream.aclose()
        await client.aclose()
        raise HTTPException(status_code=502, detail=_STORAGE_REFUSED)

    # Stop at exactly what we are about to promise, so the two can never disagree.
    # Forwarding a Content-Length and then stopping short of it hands the reader a
    # truncated PDF that claims to be whole — a broken document *and* a broken response,
    # from a single upstream that lied about its size.
    limit = promised if promised is not None else _MAX_BYTES

    async def body():
        sent = 0
        try:
            async for chunk in upstream.aiter_bytes(_CHUNK):
                if sent + len(chunk) > limit:
                    # Only reachable when upstream sends more than it said it would, or
                    # more than the cap. Either way this is the last byte we relay.
                    logger.error(
                        "Notes for %s sent more than %d bytes; stopping",
                        chapter_id, limit,
                    )
                    yield chunk[: limit - sent]
                    return
                sent += len(chunk)
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    headers = {"Cache-Control": "private, max-age=600"}
    if promised is not None:
        headers["Content-Length"] = str(promised)
    return StreamingResponse(body(), media_type="application/pdf", headers=headers)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
