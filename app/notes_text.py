"""The words inside a chapter's notes, for Ask Jeene to read.

`chapter_notes` stores a URL, a page count and a size. It has never stored the text,
because nothing needed it: the reader streams the PDF to a viewer and the student does
the reading. A doubt solver cannot do that — it can only ground an answer in material it
has actually been handed.

Extracted once per document and cached on the row, because these documents change about
as often as a syllabus does. Re-extracted when the row is touched, so a re-uploaded PDF
does not leave last term's text behind.

**What comes out is not prose.** These are visual revision notes: formulas, headings and
labels, laid out in two dimensions and flattened into one by extraction. A page reads like
`F = G m1m2 / r2 [ M-1 L3 T-2 ]`. That is genuinely useful for telling a model what a
chapter covers and in what terms — and it is not an explanation of anything. Measured on
Gravitation: 21 pages, 5,639 characters, about 1,400 tokens for the whole document, which
is small enough to send entire and skip retrieval altogether.
"""

from __future__ import annotations

import asyncio
import io
import logging

import asyncpg
import httpx
from fastapi import HTTPException

from app.storage import STORAGE_TIMEOUT, check_storage_url

logger = logging.getLogger(__name__)

#: Smaller than the streaming cap, because this one is held in memory to be parsed rather
#: than passed through a chunk at a time. The largest notes document today is 3.8 MB.
MAX_PDF_BYTES = 32 * 1024 * 1024

#: Enough for a chapter several times the size of any that exists. A document past this is
#: not revision notes, and sending it to a model would cost more than the answer is worth.
MAX_TEXT_CHARS = 120_000


async def text_for(
    connection: asyncpg.Connection, chapter_id: str, tenant: str
) -> str:
    """The chapter's notes as text, extracting and caching on first use.

    Empty string for a chapter with no notes, notes that could not be fetched, or a
    document with nothing extractable — all three are the same to a caller, which only
    wants to know what it may quote.
    """
    row = await connection.fetchrow(
        """
        SELECT pdf_url, extracted_text, text_extracted_at, updated_at
          FROM chapter_notes
         WHERE chapter_id = $1 AND tenant_id = $2 AND status = 'published'
        """,
        chapter_id, tenant,
    )
    if row is None:
        return ""

    # Extracted after the row was last written, so it is the text of the file that is
    # there now. A re-upload bumps `updated_at` and this falls through to re-extract.
    if row["text_extracted_at"] is not None and row["text_extracted_at"] >= row["updated_at"]:
        return row["extracted_text"]

    text = await _extract(row["pdf_url"], chapter_id)
    if text is None:
        # Could not fetch or could not parse. Deliberately *not* recorded as extracted:
        # storage having a bad minute must not cost this chapter its notes for ever.
        return row["extracted_text"]

    await connection.execute(
        """
        UPDATE chapter_notes
           SET extracted_text = $2, text_extracted_at = now()
         WHERE chapter_id = $1
        """,
        chapter_id, text,
    )
    if not text:
        # Parsed fine and held no text — an image-only PDF. Recorded, so it is tried once
        # rather than on every question a student asks about the chapter.
        logger.warning("notes for %s have no extractable text", chapter_id)
    return text


async def _extract(pdf_url: str, chapter_id: str) -> str | None:
    """Fetch and parse, or None if either failed. Never raises."""
    try:
        check_storage_url(pdf_url)
    except HTTPException:
        # The guard's job is to refuse a URL, and it refuses the way a request handler
        # wants — by raising a 502 at whoever asked. Nobody asked for these notes: this
        # runs while assembling material for a doubt about the chapter, and a chapter
        # whose notes URL is wrong still has concepts and worked solutions to answer
        # from. Letting the refusal out of here turned one bad row into a dead feature
        # for that whole chapter.
        logger.warning("notes for %s have a URL storage will not fetch", chapter_id)
        return None

    try:
        # Redirects off, for the same reason the reader has them off: a 302 from an
        # allow-listed host is how a checked URL becomes a fetch of something else.
        async with httpx.AsyncClient(
            timeout=STORAGE_TIMEOUT, follow_redirects=False
        ) as client:
            response = await client.get(pdf_url)
        if response.status_code != 200:
            logger.warning("notes for %s answered %s", chapter_id, response.status_code)
            return None
        if len(response.content) > MAX_PDF_BYTES:
            logger.warning("notes for %s are %d bytes", chapter_id, len(response.content))
            return None
    except httpx.HTTPError:
        logger.warning("could not fetch notes for %s", chapter_id)
        return None

    # Parsing is CPU-bound and synchronous. On the event loop it would stall every other
    # request in this worker for as long as it takes, which on a 4 MB document is not a
    # rounding error.
    try:
        return await asyncio.to_thread(_read_pdf, response.content)
    except Exception:  # noqa: BLE001 — a malformed PDF is not worth a 500
        logger.exception("could not read the notes PDF for %s", chapter_id)
        return None


def _read_pdf(raw: bytes) -> str:
    """Every page's text, joined. Runs in a worker thread."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(raw))
    pages: list[str] = []
    total = 0
    for page in reader.pages:
        text = " ".join((page.extract_text() or "").split())
        if not text:
            continue
        pages.append(text)
        total += len(text)
        if total >= MAX_TEXT_CHARS:
            break
    return "\n\n".join(pages)[:MAX_TEXT_CHARS]
