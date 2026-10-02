"""PYQ marked NCERT, as a student meets it.

A highlight is stored with its questions and nothing else; what it shows a reader — past
year or practice, which years, how many questions, how far they have got — is worked out
per request from the questions that reader may meet. These tests are mostly about that
rule, because the way it fails is quiet: a JEE year chip in front of a NEET student, a
count that includes a draft, a line that opens an empty deck.

The integration half builds its own document on the seed's `phy_11_ch8`: questions from
NEET, AIPMT and JEE Main papers, practice questions, a draft, a dated assertion-reason
question, and highlights mixing them. Everything it writes it deletes again.
"""

import asyncio
import json
import os
import uuid
from datetime import timedelta

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import assets
from app.routers.ncert import ASSET_KIND, deck_order, exam_label, is_past_year
from app.schemas import NcertDeck, Question

_DB = os.getenv("DATABASE_URL")
integration = pytest.mark.skipif(not _DB, reason="DATABASE_URL not set; these need a real database")

CHAPTER = "phy_11_ch8"
DOC = "ncd_phy_11_ch8_testncert0"
STORAGE_URL = "https://cdn.example/ncert/phy_11_ch8/ncert.testncert0.pdf"
NEET_UID = "ncert-test-neet"
JEE_UID = "ncert-test-jee"
SOLUTION = "The worked solution, which must never reach a deck."


# --- the rules, without a database ------------------------------------------------------

def test_a_past_year_question_is_one_with_a_year_whatever_its_type():
    assert is_past_year("pyq", None)
    assert is_past_year("assertion_reasoning", 2022)
    assert not is_past_year("mcq", None)


@pytest.mark.parametrize("exam, year, label", [
    ("NEET", 2019, "NEET 2019"),
    ("neet", 2020, "NEET 2020"),
    ("NEET 2016 Phase I", 2016, "NEET 2016"),
    ("aipmt", 2015, "AIPMT 2015"),
    ("CBSE-AIPMT", 2012, "AIPMT 2012"),
    ("JEE Main", 2021, "JEE Main 2021"),
    ("JEE Advanced", 2019, "JEE Advanced 2019"),
    ("KVPY", 2018, "KVPY 2018"),
    (None, 2017, "PYQ 2017"),
    ("NEET", None, None),
])
def test_a_year_chip_names_the_exam_the_way_a_student_knows_it(exam, year, label):
    assert exam_label(exam, year) == label


def test_a_deck_puts_past_papers_first_newest_first_then_easy_to_hard():
    rows = [
        {"question_id": "m_hard", "question_type": "mcq", "pyq_year": None, "difficulty": "hard"},
        {"question_id": "p_2015", "question_type": "pyq", "pyq_year": 2015, "difficulty": None},
        {"question_id": "m_unrated", "question_type": "mcq", "pyq_year": None, "difficulty": None},
        {"question_id": "p_undated", "question_type": "pyq", "pyq_year": None, "difficulty": "easy"},
        {"question_id": "m_easy", "question_type": "mcq", "pyq_year": None, "difficulty": "easy"},
        {"question_id": "ar_2022", "question_type": "assertion_reasoning", "pyq_year": 2022,
         "difficulty": "hard"},
    ]
    assert deck_order(rows) == ["ar_2022", "p_2015", "p_undated", "m_easy", "m_hard", "m_unrated"]


def test_a_document_link_opens_nothing_else():
    token = assets.sign(ASSET_KIND, DOC)
    assert assets.verify(ASSET_KIND, DOC, token)
    assert not assets.verify("notes", DOC, token)
    assert not assets.verify(ASSET_KIND, "ncd_some_other_doc", token)


def test_a_deck_has_nowhere_to_put_an_answer():
    """Answer integrity is a rule in CLAUDE.md, not a habit. The deck is built from
    `Question`, which has no field that could carry a key or a solution."""
    fields = set(NcertDeck.model_fields) | set(Question.model_fields)
    assert not fields & {"correct_option_ids", "explanation", "explanation_json", "answer"}


# --- the fixture ----------------------------------------------------------------------

QUESTIONS = {
    # id: (type, exam, year, difficulty, status)
    "ncert_t_pyq_neet_2019": ("pyq", "NEET", 2019, "hard", "published"),
    "ncert_t_pyq_aipmt_2015": ("pyq", "aipmt", 2015, "medium", "published"),
    "ncert_t_pyq_jee_2021": ("pyq", "JEE Main", 2021, "easy", "published"),
    "ncert_t_pyq_undated": ("pyq", None, None, "easy", "published"),
    "ncert_t_ar_neet_2022": ("assertion_reasoning", "NEET", 2022, "medium", "published"),
    "ncert_t_mcq_easy": ("mcq", None, None, "easy", "published"),
    "ncert_t_mcq_hard": ("mcq", None, None, "hard", "published"),
    "ncert_t_mcq_draft": ("mcq", None, None, "easy", "draft"),
}

HIGHLIGHTS = {
    # id: (status, page, top, questions)
    "nh_test_mixed": ("published", 0, 0.20, ["ncert_t_pyq_neet_2019", "ncert_t_pyq_aipmt_2015",
                                              "ncert_t_pyq_jee_2021", "ncert_t_mcq_easy",
                                              "ncert_t_mcq_hard"]),
    "nh_test_jee_only": ("published", 0, 0.50, ["ncert_t_pyq_jee_2021"]),
    "nh_test_practice": ("published", 1, 0.10, ["ncert_t_mcq_easy", "ncert_t_mcq_draft"]),
    "nh_test_dated_ar": ("published", 1, 0.30, ["ncert_t_ar_neet_2022", "ncert_t_pyq_undated"]),
    "nh_test_draft": ("draft", 1, 0.60, ["ncert_t_mcq_hard"]),
}


async def _clean(connection):
    ids = list(QUESTIONS)
    await connection.execute("DELETE FROM attempts WHERE question_id = ANY($1::text[])", ids)
    await connection.execute("DELETE FROM ncert_documents WHERE doc_id = $1", DOC)
    await connection.execute("DELETE FROM questions WHERE question_id = ANY($1::text[])", ids)
    await connection.execute(
        "DELETE FROM users WHERE firebase_uid = ANY($1::text[])", [NEET_UID, JEE_UID]
    )


async def _build():
    connection = await asyncpg.connect(_DB)
    try:
        await _clean(connection)
        await connection.execute(
            "INSERT INTO users (firebase_uid, tenant_id, target_exam) VALUES "
            "($1, 'JEENE_MASTER', 'NEET'), ($2, 'JEENE_MASTER', 'JEE Main')",
            NEET_UID, JEE_UID,
        )
        for qid, (qtype, exam, year, difficulty, status) in QUESTIONS.items():
            await connection.execute(
                """
                INSERT INTO questions (question_id, tenant_id, question_type, question_text,
                       options_json, correct_option_ids, explanation_json, pyq_exam, pyq_year,
                       difficulty, status, source)
                VALUES ($1, 'JEENE_MASTER', $2, $3, $4::jsonb, ARRAY['a'], $5::jsonb, $6, $7,
                        $8, $9, 'in-house')
                """,
                qid, qtype, f"Question {qid}?",
                json.dumps([{"id": "a", "text": "this"}, {"id": "b", "text": "that"}]),
                json.dumps({"text": SOLUTION}), exam, year, difficulty, status,
            )
        page = {"w": 603.4, "h": 793.5, "column": [0.08, 0.86]}
        await connection.execute(
            """
            INSERT INTO ncert_documents (doc_id, tenant_id, chapter_node_id, sha256, pdf_url,
                   page_count, pages, status)
            VALUES ($1, 'JEENE_MASTER', $2, $3, $4, 2, $5::jsonb, 'published')
            """,
            DOC, CHAPTER, "f" * 64, STORAGE_URL, json.dumps([page, page]),
        )
        for hid, (status, page_no, top, questions) in HIGHLIGHTS.items():
            rects = [{"page": page_no, "x0": 0.1, "y0": top, "x1": 0.8, "y1": top + 0.015}]
            await connection.execute(
                """
                INSERT INTO ncert_highlights (highlight_id, doc_id, rects, quote, section,
                       first_page, top, status)
                VALUES ($1, $2, $3::jsonb, $4, '8.2', $5, $6, $7)
                """,
                hid, DOC, json.dumps(rects), f"The line {hid} highlights.", page_no, top, status,
            )
            for qid in questions:
                await connection.execute(
                    "INSERT INTO ncert_highlight_questions (highlight_id, question_id, relation, "
                    "confidence) VALUES ($1, $2, 'basis', 0.9)",
                    hid, qid,
                )
        # The NEET student's record: one past-year question wrong and then right (the
        # latest counts), the easy one right, the hard one wrong.
        for qid, ok, hours in [("ncert_t_pyq_neet_2019", False, 2),
                               ("ncert_t_pyq_neet_2019", True, 1),
                               ("ncert_t_mcq_easy", True, 1),
                               ("ncert_t_mcq_hard", False, 1)]:
            await connection.execute(
                "INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id, "
                "is_correct, created_at) VALUES ($1, $2, 'JEENE_MASTER', $3, $4, "
                "now() - $5::interval)",
                uuid.uuid4(), NEET_UID, qid, ok, timedelta(hours=hours),
            )
    finally:
        await connection.close()


async def _teardown():
    connection = await asyncpg.connect(_DB)
    try:
        await _clean(connection)
    finally:
        await connection.close()


async def _set_document_status(status: str):
    connection = await asyncpg.connect(_DB)
    try:
        await connection.execute("UPDATE ncert_documents SET status = $1 WHERE doc_id = $2",
                                 status, DOC)
    finally:
        await connection.close()


@pytest.fixture(scope="module")
def client():
    if not _DB:
        pytest.skip("DATABASE_URL not set")
    from app.auth import current_tenant, optional_user, require_user
    from app.main import app

    asyncio.run(_build())
    app.dependency_overrides[current_tenant] = lambda: "JEENE_MASTER"
    app.dependency_overrides[require_user] = lambda: {"uid": NEET_UID}
    app.dependency_overrides[optional_user] = lambda: {"uid": NEET_UID}
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.clear()
        asyncio.run(_teardown())


@pytest.fixture
def reader():
    """Who is reading, for the duration of one test."""
    from app.auth import optional_user
    from app.main import app

    def as_(uid):
        app.dependency_overrides[optional_user] = (lambda: {"uid": uid}) if uid else (lambda: None)

    yield as_
    app.dependency_overrides[optional_user] = lambda: {"uid": NEET_UID}


def _by_id(body):
    return {h["highlight_id"]: h for h in body["highlights"]}


# --- the document -----------------------------------------------------------------------

@integration
def test_a_neet_student_sees_the_lines_their_questions_came_from(client):
    response = client.get(f"/chapters/{CHAPTER}/ncert")
    assert response.status_code == 200
    body = response.json()

    # Reading order, and only what this reader may open: the JEE-only line and the draft
    # line are not there at all.
    assert [h["highlight_id"] for h in body["highlights"]] == [
        "nh_test_mixed", "nh_test_practice", "nh_test_dated_ar"]
    by_id = _by_id(body)

    mixed = by_id["nh_test_mixed"]
    assert mixed["kind"] == "pyq"
    assert mixed["years"] == ["NEET 2019", "AIPMT 2015"]
    assert mixed["question_count"] == 4          # the JEE question is not counted
    assert (mixed["answered"], mixed["correct"]) == (3, 2)

    practice = by_id["nh_test_practice"]
    assert practice["kind"] == "practice"
    assert practice["question_count"] == 1       # the draft is not counted
    assert (practice["answered"], practice["correct"]) == (1, 1)

    dated = by_id["nh_test_dated_ar"]
    assert dated["kind"] == "pyq"                # a dated assertion-reason, an undated pyq
    assert dated["years"] == ["NEET 2022"]
    assert dated["question_count"] == 2

    assert body["page_count"] == 2
    assert body["pages"][0] == {"w": 603.4, "h": 793.5, "column": [0.08, 0.86]}
    assert mixed["rects"][0]["page"] == 0


@integration
def test_a_jee_student_sees_jee_years_and_never_neet_ones(client, reader):
    reader(JEE_UID)
    by_id = _by_id(client.get(f"/chapters/{CHAPTER}/ncert").json())

    assert by_id["nh_test_mixed"]["years"] == ["JEE Main 2021"]
    assert by_id["nh_test_mixed"]["question_count"] == 3
    assert by_id["nh_test_jee_only"]["kind"] == "pyq"
    # NEET's dated assertion-reason question is gone; its undated pyq stays, for everyone.
    assert by_id["nh_test_dated_ar"]["question_count"] == 1
    assert by_id["nh_test_dated_ar"]["years"] == []
    # And nothing of the NEET student's progress leaks across.
    assert all(h["answered"] == 0 for h in by_id.values())


@integration
def test_signed_out_reading_is_scoped_to_the_catalogue_exam(client, reader):
    reader(None)
    response = client.get(f"/chapters/{CHAPTER}/ncert")
    assert response.status_code == 200
    by_id = _by_id(response.json())
    assert "nh_test_jee_only" not in by_id
    assert by_id["nh_test_mixed"]["years"] == ["NEET 2019", "AIPMT 2015"]
    assert all(h["answered"] == 0 and h["correct"] == 0 for h in by_id.values())


@integration
def test_the_storage_url_never_reaches_the_client(client):
    response = client.get(f"/chapters/{CHAPTER}/ncert")
    assert "cdn.example" not in response.text
    assert "pdf_url" not in response.json()
    assert f"/ncert/{DOC}/file?t=" in response.json()["file_url"]


@integration
def test_an_unpublished_document_is_not_there(client):
    asyncio.run(_set_document_status("draft"))
    try:
        assert client.get(f"/chapters/{CHAPTER}/ncert").status_code == 404
        assert client.get("/ncert/highlights/nh_test_mixed/questions").status_code == 404
    finally:
        asyncio.run(_set_document_status("published"))


@integration
def test_a_chapter_without_a_marked_ncert_is_a_404(client):
    assert client.get("/chapters/phy_11_ch5/ncert").status_code == 404


# --- the deck -----------------------------------------------------------------------------

@integration
def test_the_deck_is_past_papers_newest_first_then_practice_easy_to_hard(client):
    response = client.get("/ncert/highlights/nh_test_mixed/questions")
    assert response.status_code == 200
    body = response.json()
    assert [q["question_id"] for q in body["questions"]] == [
        "ncert_t_pyq_neet_2019", "ncert_t_pyq_aipmt_2015", "ncert_t_mcq_easy", "ncert_t_mcq_hard"]
    assert body["labels"] == {"ncert_t_pyq_neet_2019": "NEET 2019",
                              "ncert_t_pyq_aipmt_2015": "AIPMT 2015"}
    assert body["chapter_id"] == CHAPTER


@integration
def test_no_answer_ever_reaches_the_deck(client):
    for hid in ("nh_test_mixed", "nh_test_practice", "nh_test_dated_ar"):
        text = client.get(f"/ncert/highlights/{hid}/questions").text
        assert SOLUTION not in text
        assert "correct_option_ids" not in text
        assert "explanation" not in text


@integration
def test_a_line_with_nothing_for_this_reader_does_not_open(client, reader):
    # Every question on it is from another exam.
    assert client.get("/ncert/highlights/nh_test_jee_only/questions").status_code == 404
    # A draft line, whatever is on it.
    assert client.get("/ncert/highlights/nh_test_draft/questions").status_code == 404
    reader(JEE_UID)
    assert client.get("/ncert/highlights/nh_test_jee_only/questions").status_code == 200


@integration
def test_a_draft_question_is_never_dealt(client):
    ids = [q["question_id"] for q in
           client.get("/ncert/highlights/nh_test_practice/questions").json()["questions"]]
    assert ids == ["ncert_t_mcq_easy"]


# --- the file -------------------------------------------------------------------------------

@integration
def test_the_file_refuses_an_unsigned_or_expired_link(client):
    assert client.get(f"/ncert/{DOC}/file").status_code == 403
    assert client.get(f"/ncert/{DOC}/file", params={"t": "nonsense"}).status_code == 403
    expired = assets.sign(ASSET_KIND, DOC, ttl_seconds=-1)
    assert client.get(f"/ncert/{DOC}/file", params={"t": expired}).status_code == 403
    # A notes token is not an NCERT token, even for the same id.
    notes = assets.sign("notes", DOC)
    assert client.get(f"/ncert/{DOC}/file", params={"t": notes}).status_code == 403


@integration
def test_the_file_streams_from_storage_without_naming_it(client, monkeypatch):
    import httpx

    from app.config import settings

    body = b"%PDF-1.7\n" + b"n" * 8192
    asked: list[str] = []

    def storage(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, content=body, headers={"content-length": str(len(body))})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(storage), **kw))
    monkeypatch.setattr(settings, "jeene_notes_storage_hosts", "cdn.example")

    file_url = client.get(f"/chapters/{CHAPTER}/ncert").json()["file_url"]
    path = "/" + file_url.split("://", 1)[1].split("/", 1)[1]
    response = client.get(path)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == body
    assert asked == [STORAGE_URL]


@integration
def test_storage_refusing_is_a_502_not_a_traceback(client, monkeypatch):
    import httpx

    from app.config import settings

    def storage(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(storage), **kw))
    monkeypatch.setattr(settings, "jeene_notes_storage_hosts", "cdn.example")
    token = assets.sign(ASSET_KIND, DOC)
    assert client.get(f"/ncert/{DOC}/file", params={"t": token}).status_code == 502
