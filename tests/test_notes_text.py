"""Reading the words out of a chapter's notes.

Ask Jeene may only ground an answer in material it has been handed, and until now the
notes were a URL and a page count — nothing a model could quote. This is the extraction
that changes that, and the three things it must get right are all about *not* being
clever: fetch only what the storage guard allows, never block the event loop on a parse,
and never let a bad minute at storage cost a chapter its notes for ever.
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid

import asyncpg
import pytest

from app import notes_text

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="the cache lives in the database"
)

TENANT = "JEENE_MASTER"


def _a_pdf(pages: list[str]) -> bytes:
    """A real, valid PDF carrying real text, assembled here.

    Hand-rolled rather than pulling in a PDF author just to write a test file. It is forty
    lines of a very old and very stable format, and it means the parse below is exercised
    on every run instead of skipping wherever that library is missing.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    content_ids: list[int] = []
    for text in pages:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 24 Tf 72 720 Td ({escaped}) Tj ET".encode()
        content_ids.append(add(b"<< /Length %d >>\nstream\n" % len(stream) + stream
                               + b"\nendstream"))
        page_ids.append(0)  # filled once the Pages object has a number

    pages_id = len(objects) + len(pages) + 1
    for index, content in enumerate(content_ids):
        page_ids[index] = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font, content)
        )
    kids = b" ".join(b"%d 0 R" % i for i in page_ids)
    add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids)))
    catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"

    start = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF"
            % (len(objects) + 1, catalog, start))
    return bytes(out)


def in_tx(body):
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        tx = conn.transaction()
        await tx.start()
        try:
            return await body(conn)
        finally:
            await tx.rollback()
            await conn.close()
    return asyncio.run(go())


async def _notes(conn, *, chapter: str, url: str = "https://img.example.test/n.pdf") -> None:
    await conn.execute(
        """INSERT INTO nodes (node_id, tenant_id, type, title, slug, depth, status)
           VALUES ($1, $2, 'chapter', 'A chapter', $1, 1, 'published')
           ON CONFLICT (node_id) DO NOTHING""", chapter, TENANT)
    await conn.execute(
        """INSERT INTO chapter_notes (chapter_id, tenant_id, title, pdf_url, status)
           VALUES ($1, $2, 'Notes', $3, 'published')""", chapter, TENANT, url)


# --- the cache -------------------------------------------------------------------------


def test_a_chapter_with_no_notes_has_nothing_to_quote():
    async def body(conn):
        assert await notes_text.text_for(conn, "no_such_chapter", TENANT) == ""
    in_tx(body)


def test_text_is_extracted_once_and_then_read_from_the_row(monkeypatch):
    """These documents change about as often as a syllabus. Fetching per question would
    be a megabyte of storage traffic for every doubt asked about the chapter."""
    async def body(conn):
        chapter = f"test_nt_{uuid.uuid4().hex[:8]}"
        await _notes(conn, chapter=chapter)
        fetches = []

        async def once(url, chapter_id):
            fetches.append(url)
            return "NEWTON'S LAW OF GRAVITATION  F = G m1m2 / r2"

        monkeypatch.setattr(notes_text, "_extract", once)

        first = await notes_text.text_for(conn, chapter, TENANT)
        second = await notes_text.text_for(conn, chapter, TENANT)

        assert "GRAVITATION" in first
        assert second == first
        assert len(fetches) == 1, "the second read came from the row"
    in_tx(body)


def test_a_re_uploaded_document_is_read_again(monkeypatch):
    """Otherwise a chapter keeps last term's notes for ever, silently."""
    async def body(conn):
        chapter = f"test_nt_{uuid.uuid4().hex[:8]}"
        await _notes(conn, chapter=chapter)
        answers = ["first version", "second version"]

        async def each(url, chapter_id):
            return answers.pop(0)

        monkeypatch.setattr(notes_text, "_extract", each)
        assert await notes_text.text_for(conn, chapter, TENANT) == "first version"

        await conn.execute(
            "UPDATE chapter_notes SET updated_at = now() + interval '1 second' "
            "WHERE chapter_id = $1", chapter)

        assert await notes_text.text_for(conn, chapter, TENANT) == "second version"
    in_tx(body)


def test_storage_having_a_bad_minute_does_not_cost_the_chapter_its_notes(monkeypatch):
    """A failure is not an answer. Recording it as one would mean a transient outage
    left the chapter permanently unquotable."""
    async def body(conn):
        chapter = f"test_nt_{uuid.uuid4().hex[:8]}"
        await _notes(conn, chapter=chapter)

        async def down(url, chapter_id):
            return None

        monkeypatch.setattr(notes_text, "_extract", down)
        assert await notes_text.text_for(conn, chapter, TENANT) == ""

        stamped = await conn.fetchval(
            "SELECT text_extracted_at FROM chapter_notes WHERE chapter_id = $1", chapter)
        assert stamped is None, "not recorded, so the next question tries again"
    in_tx(body)


def test_a_document_with_no_text_is_recorded_so_it_is_tried_once(monkeypatch):
    """An image-only PDF parses perfectly and holds nothing. That is a settled answer,
    and retrying it on every question about the chapter is pure waste."""
    async def body(conn):
        chapter = f"test_nt_{uuid.uuid4().hex[:8]}"
        await _notes(conn, chapter=chapter)
        tries = []

        async def blank(url, chapter_id):
            tries.append(url)
            return ""

        monkeypatch.setattr(notes_text, "_extract", blank)
        await notes_text.text_for(conn, chapter, TENANT)
        await notes_text.text_for(conn, chapter, TENANT)

        assert len(tries) == 1
        stamped = await conn.fetchval(
            "SELECT text_extracted_at FROM chapter_notes WHERE chapter_id = $1", chapter)
        assert stamped is not None
    in_tx(body)


# --- the parse ---------------------------------------------------------------------------


def test_a_real_pdf_is_read_page_by_page():
    raw = _a_pdf(["Newtons law of gravitation", "Escape velocity"])
    text = notes_text._read_pdf(raw)
    assert "gravitation" in text.lower()
    assert "Escape velocity" in text


def test_a_document_that_is_not_a_pdf_is_refused_rather_than_raising(monkeypatch):
    async def body(conn):
        chapter = f"test_nt_{uuid.uuid4().hex[:8]}"
        await _notes(conn, chapter=chapter)

        class Reply:
            status_code = 200
            content = b"this is not a pdf"

        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url): return Reply()

        monkeypatch.setattr(notes_text.httpx, "AsyncClient", lambda **kw: Client())
        monkeypatch.setattr(notes_text, "check_storage_url", lambda url: None)

        assert await notes_text.text_for(conn, chapter, TENANT) == ""
    in_tx(body)


def test_an_internal_url_is_never_fetched(monkeypatch):
    """The same guard the reader uses. `pdf_url` is written by another repository, and
    that is a fact about someone else's code rather than a property of this one.

    What is asserted is that no request leaves the process — not how the refusal is
    spelled. The first version of this test pinned the spelling instead, and so pinned a
    bug: `_extract` declined by raising, which took the whole doubt request down with it.
    """
    def never(*a, **k):
        raise AssertionError("a request was made for an internal URL")

    monkeypatch.setattr(notes_text.httpx, "AsyncClient", never)

    async def body(conn):
        chapter = f"test_nt_{uuid.uuid4().hex[:8]}"
        assert await notes_text._extract("http://127.0.0.1:8000/health", chapter) is None
    in_tx(body)


def test_a_url_storage_refuses_costs_the_notes_not_the_feature():
    """The storage guard refuses a URL by raising a 502 at whoever asked. Nobody asked
    for these notes — this runs while assembling material for a doubt — so a chapter with
    one bad URL must still be answerable from its concepts and worked solutions.

    Found by running a real doubt: every question about that chapter died with "That
    document is not available", and the chapter had plenty else to answer from.
    """
    async def body(conn):
        chapter = f"test_nt_{uuid.uuid4().hex[:8]}"
        await _notes(conn, chapter=chapter, url="http://169.254.169.254/latest/meta-data")

        assert await notes_text.text_for(conn, chapter, TENANT) == ""

        # And not recorded as extracted, so fixing the row fixes the chapter.
        recorded = await conn.fetchval(
            "SELECT text_extracted_at FROM chapter_notes WHERE chapter_id = $1", chapter)
        assert recorded is None
    in_tx(body)
