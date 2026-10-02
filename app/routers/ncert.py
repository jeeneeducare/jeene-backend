"""PYQ marked NCERT: a chapter's NCERT, with the lines its questions came from.

A student reads the chapter's own NCERT in the app, and some lines are highlighted: amber
where a past-year question was asked from that line, blue where practice questions were.
Tapping one opens those questions. Three routes:

  * `/chapters/{id}/ncert` — the document, a signed link to its file, and its highlights
    as this reader may see them, with their progress through each;
  * `/ncert/{doc_id}/file` — the PDF, streamed through this process under that signature;
  * `/ncert/highlights/{id}/questions` — the deck behind one highlight, without answers.

The content pipeline (`/jeene:mark-ncert` in jeene-plugin) decides which lines and which
questions, and writes `ncert_documents`, `ncert_highlights` and
`ncert_highlight_questions`. This router only reads them.

**What a highlight is, is decided per reader.** A highlight is stored with its questions
and nothing else: whether it is a past-year line, which years it shows, and how many
questions it opens are all worked out here from the questions this reader may meet —
published, not held back with an unreleased paper, and from the exam they are sitting. So a
JEE year never shows to a NEET student, a draft question never inflates a count, and a
highlight whose every question is hidden from this reader is not shown at all: a line that
opens an empty deck is worse than no line.

**Reading is free; answering is practice.** Nothing here is behind the chapter lock: the
NCERT is the textbook, and the decision was that reading it costs nothing. Answering goes
through `POST /attempts`, which already counts the free daily allowance.
"""

from __future__ import annotations

import collections
import json

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from app import assets
from app.auth import current_tenant, optional_user
from app.db import get_connection
from app.questions import fetch_questions_by_ids
from app.schemas import ChapterNcert, NcertDeck, NcertHighlight, NcertPage, NcertRect
from app.storage import stream_document
from app.visibility import NOT_UNRELEASED_TEST_SQL, exam_scope_sql, resolve_exam_track

router = APIRouter()

#: What a signed NCERT link is for, so it cannot be replayed against another asset kind.
ASSET_KIND = "ncert"

#: Easy before hard in the practice half of a deck; a question nobody has graded goes last.
_DIFFICULTY_ORDER = {"easy": 0, "medium": 1, "hard": 2}

_DOCUMENT_SQL = """
    SELECT d.doc_id, d.chapter_node_id, d.sha256, d.page_count, d.pages, d.pdf_url
      FROM ncert_documents d
      JOIN nodes c ON c.node_id = d.chapter_node_id
     WHERE d.tenant_id = $1 AND d.status = 'published' AND c.status = 'published'
"""

# Every highlight of a document with every question of it this reader may meet. A
# highlight none of whose questions survive the filters has no row here, and so is not
# shown. $3 is the reader's exam track.
#
# In reading order: page by page, and on a page down the left column before the right,
# because NCERT is set in two columns and "next line" should mean the next one a reader
# would reach, not whichever starts a little higher across the gutter.
_VISIBLE_LINKS_SQL = f"""
    SELECT h.highlight_id, h.rects, h.quote, h.section, h.first_page, h.top,
           q.question_id, q.question_type, q.pyq_exam, q.pyq_year
      FROM ncert_highlights h
      JOIN ncert_highlight_questions l ON l.highlight_id = h.highlight_id
      JOIN questions q ON q.question_id = l.question_id
     WHERE h.doc_id = $1 AND h.status = 'published'
       AND q.tenant_id = $2 AND q.status = 'published'
       {NOT_UNRELEASED_TEST_SQL}
       {exam_scope_sql(3)}
     ORDER BY h.first_page, (h.rects->0->>'x0')::float >= 0.5, h.top, h.highlight_id,
              q.question_id
"""


def _jsonb(value):
    """asyncpg hands JSONB back as text unless a codec is registered; accept both."""
    return json.loads(value) if isinstance(value, str) else value


def is_past_year(question_type: str | None, year: int | None) -> bool:
    """A past paper's assertion-reason question is typed `assertion_reasoning`, not
    `pyq`, so a question with an exam year counts as past-year whatever its type."""
    return question_type == "pyq" or year is not None


def exam_label(exam: str | None, year: int | None) -> str | None:
    """"NEET 2019" for a past-year question that carries its year, else None.

    `pyq_exam` is free text written over several years (NEET, neet, aipmt, CBSE-AIPMT,
    "NEET 2016 Phase I", "JEE Main"), so the chip names the exam's family the way a
    student knows it rather than echoing whichever spelling the bank used. AIPMT keeps its
    own name: it is what the paper was called the year it was set.
    """
    if year is None:
        return None
    raw = (exam or "").strip()
    lowered = raw.lower()
    if "jee" in lowered:
        name = "JEE Advanced" if "adv" in lowered else "JEE Main" if "main" in lowered else "JEE"
    elif "aipmt" in lowered or "pmt" in lowered:
        name = "AIPMT"
    elif "neet" in lowered:
        name = "NEET"
    else:
        name = raw or "PYQ"
    return f"{name} {year}"


def deck_order(rows: list[dict]) -> list[str]:
    """Past-year questions first, newest year first; then practice, easy to hard.

    A past-year question with no recorded year still counts as past-year, after the dated
    ones. Ties break on question id, so the order is the same every time it is asked for.
    """
    def key(r):
        if is_past_year(r["question_type"], r["pyq_year"]):
            return (0, -(r["pyq_year"] or 0), 0, r["question_id"])
        return (1, 0, _DIFFICULTY_ORDER.get(r["difficulty"] or "", 3), r["question_id"])

    return [r["question_id"] for r in sorted(rows, key=key)]


async def _latest_attempts(
    connection: asyncpg.Connection, uid: str, tenant: str, question_ids: list[str]
) -> dict[str, bool]:
    """Whether this student's most recent attempt at each question was right.

    The most recent, as on the chapter's history: answering correctly replaces the record
    of getting it wrong.
    """
    if not question_ids:
        return {}
    rows = await connection.fetch(
        """
        SELECT DISTINCT ON (a.question_id) a.question_id, a.is_correct
          FROM attempts a
         WHERE a.firebase_uid = $1 AND a.tenant_id = $2
           AND a.question_id = ANY($3::text[])
         ORDER BY a.question_id, a.created_at DESC
        """,
        uid, tenant, question_ids,
    )
    return {r["question_id"]: r["is_correct"] for r in rows}


@router.get("/chapters/{chapter_id}/ncert", response_model=ChapterNcert)
async def chapter_ncert(
    chapter_id: str,
    request: Request,
    tenant: str = Depends(current_tenant),
    user: dict | None = Depends(optional_user),
    connection: asyncpg.Connection = Depends(get_connection),
) -> ChapterNcert:
    """A chapter's marked NCERT, if it has one published.

    404 when it has none, which the app asks about when a chapter opens so the button
    only appears where there is something behind it.
    """
    doc = await connection.fetchrow(
        _DOCUMENT_SQL + " AND d.chapter_node_id = $2", tenant, chapter_id
    )
    if doc is None:
        raise HTTPException(status_code=404, detail="This chapter's NCERT is not marked yet")

    uid = (user or {}).get("uid")
    track = await resolve_exam_track(connection, tenant, uid)
    rows = await connection.fetch(_VISIBLE_LINKS_SQL, doc["doc_id"], tenant, track)

    grouped: dict[str, list[asyncpg.Record]] = collections.OrderedDict()
    for r in rows:
        grouped.setdefault(r["highlight_id"], []).append(r)
    attempts = (
        await _latest_attempts(
            connection, uid, tenant, sorted({r["question_id"] for r in rows})
        )
        if uid else {}
    )

    highlights = []
    for highlight_id, links in grouped.items():
        first = links[0]
        dated = sorted(
            {(r["pyq_year"], exam_label(r["pyq_exam"], r["pyq_year"]))
             for r in links if r["pyq_year"] is not None},
            key=lambda pair: (-pair[0], pair[1]),
        )
        answered = [attempts[r["question_id"]] for r in links if r["question_id"] in attempts]
        highlights.append(NcertHighlight(
            highlight_id=highlight_id,
            kind="pyq" if any(is_past_year(r["question_type"], r["pyq_year"]) for r in links)
            else "practice",
            years=list(dict.fromkeys(label for _, label in dated)),
            question_count=len(links),
            answered=len(answered),
            correct=sum(1 for ok in answered if ok),
            first_page=first["first_page"],
            top=first["top"],
            rects=[NcertRect(**rect) for rect in _jsonb(first["rects"])],
            quote=first["quote"],
            section=first["section"],
        ))

    origin = str(request.base_url).rstrip("/")
    token = assets.sign(ASSET_KIND, doc["doc_id"])
    return ChapterNcert(
        doc_id=doc["doc_id"],
        chapter_id=doc["chapter_node_id"],
        sha256=doc["sha256"],
        page_count=doc["page_count"],
        pages=[NcertPage(**page) for page in _jsonb(doc["pages"])],
        file_url=f"{origin}/ncert/{doc['doc_id']}/file?t={token}",
        highlights=highlights,
    )


@router.get("/ncert/{doc_id}/file")
async def ncert_file(
    doc_id: str,
    t: str = Query(default=""),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> StreamingResponse:
    """The PDF, streamed through this process under the signature `/chapters/{id}/ncert`
    minted. The storage URL never leaves the server.

    Cached privately for a day: a document is one printing, content-addressed, and never
    changes under its id.
    """
    if not assets.verify(ASSET_KIND, doc_id, t):
        raise HTTPException(status_code=403, detail="This link has expired")
    doc = await connection.fetchrow(_DOCUMENT_SQL + " AND d.doc_id = $2", tenant, doc_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="This document is not available")
    return await stream_document(
        doc["pdf_url"], what=f"NCERT {doc_id}", cache_control="private, max-age=86400"
    )


@router.get("/ncert/highlights/{highlight_id}/questions", response_model=NcertDeck)
async def highlight_questions(
    highlight_id: str,
    tenant: str = Depends(current_tenant),
    user: dict | None = Depends(optional_user),
    connection: asyncpg.Connection = Depends(get_connection),
) -> NcertDeck:
    """The questions a highlight opens, as this reader may meet them, without answers.

    Fetched through `fetch_questions_by_ids`, the one reader that already knows the rules
    a question must pass and the columns it must never select. 404 when none survive:
    the app does not draw such a highlight, so asking for it means a stale screen.
    """
    head = await connection.fetchrow(
        """
        SELECT h.highlight_id, h.quote, d.chapter_node_id
          FROM ncert_highlights h
          JOIN ncert_documents d ON d.doc_id = h.doc_id
          JOIN nodes c ON c.node_id = d.chapter_node_id
         WHERE h.highlight_id = $1 AND h.status = 'published'
           AND d.tenant_id = $2 AND d.status = 'published' AND c.status = 'published'
        """,
        highlight_id, tenant,
    )
    if head is None:
        raise HTTPException(status_code=404, detail="This highlight is not available")

    linked = await connection.fetch(
        """
        SELECT q.question_id, q.question_type, q.pyq_exam, q.pyq_year, q.difficulty
          FROM ncert_highlight_questions l
          JOIN questions q ON q.question_id = l.question_id
         WHERE l.highlight_id = $1 AND q.tenant_id = $2
        """,
        highlight_id, tenant,
    )
    track = await resolve_exam_track(connection, tenant, (user or {}).get("uid"))
    questions = await fetch_questions_by_ids(
        connection, tenant, deck_order([dict(r) for r in linked]), track
    )
    if not questions:
        raise HTTPException(status_code=404, detail="This highlight has no questions for you")

    shown = {q.question_id for q in questions}
    labels = {
        r["question_id"]: label
        for r in linked
        if r["question_id"] in shown
        and (label := exam_label(r["pyq_exam"], r["pyq_year"])) is not None
    }
    return NcertDeck(
        highlight_id=head["highlight_id"],
        chapter_id=head["chapter_node_id"],
        quote=head["quote"],
        questions=questions,
        labels=labels,
    )
