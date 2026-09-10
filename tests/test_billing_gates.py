"""What is free, what Pro unlocks, and what a locked door says.

The apps hide and dim things. These tests are about the half that matters: what happens
when somebody asks anyway. Every assertion here is made with an HTTP client that has no
idea a button was supposed to be greyed out.

Four gates, and for each one both answers — the free student refused, the Pro student
through. Plus the three cases where a gate must *not* fire, which are the ones that would
be found by a furious student rather than by a test:

  * **resuming a paper already in progress**, when Pro ran out mid-sitting;
  * **reopening a plan already made**, which is their own saved work;
  * **retrying an answer already recorded**, which must not be refused for being the
    twenty-first row when it is the same row as before.

And the switch: with billing off, nothing is locked at all. A paywall on a deployment
that cannot sell Pro is a dead end, so the two turn on together.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app import db
from app.billing import gates

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="every gate reads the database"
)

TENANT = "JEENE_MASTER"
FREE = "test-gate-free"
PRO = "test-gate-pro"

SUBJECT = "test_gate_subject"
OPEN_CHAPTER = "test_gate_ch_open"     # first of its subject, so free to everybody
LOCKED_CHAPTER = "test_gate_ch_locked"  # second, so Pro
LOCKED_TOPIC = "test_gate_topic_locked"  # under the locked chapter
TEST_ID = "test_gate_paper"

#: Whose request this is. Mutable so one client fixture can be any of the three callers a
#: gate has to tell apart: signed out, free, Pro.
CALLER: dict = {"uid": FREE}


def _sql(statement, *args):
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await db._init_connection(conn)
        try:
            return await conn.fetch(statement, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


def _make_pro(uid: str, days: int = 30) -> None:
    """Grant access through the real granting code, cache and all."""
    from app.billing import entitlements

    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await db._init_connection(conn)
        try:
            await entitlements.grant(
                conn, uid=uid, tenant=TENANT, days=days, provider="test",
                note="gate tests",
            )
        finally:
            await conn.close()
    asyncio.run(go())


@pytest.fixture(scope="module", autouse=True)
def world():
    """Two students, a subject with two chapters, notes, videos and a paper."""
    for uid in (FREE, PRO):
        _sql("""INSERT INTO users (firebase_uid, tenant_id) VALUES ($1, $2)
                ON CONFLICT (firebase_uid) DO NOTHING""", uid, TENANT)
    _make_pro(PRO)

    _sql("""INSERT INTO nodes (node_id, tenant_id, type, title, slug, depth, status,
                               subject_id)
            VALUES ($1, $2, 'subject', 'Gate Subject', 'gate-subject', 0, 'published', $1)
            ON CONFLICT (node_id) DO NOTHING""", SUBJECT, TENANT)
    for node_id, number, title in (
        (OPEN_CHAPTER, 1, "The free one"), (LOCKED_CHAPTER, 2, "The paid one"),
    ):
        _sql("""INSERT INTO nodes (node_id, tenant_id, type, title, slug, depth, status,
                                   parent_id, subject_id, class_level,
                                   ncert_chapter_number)
                VALUES ($1, $2, 'chapter', $3, $1, 1, 'published', $4, $5, 11, $6)
                ON CONFLICT (node_id) DO NOTHING""",
             node_id, TENANT, title, SUBJECT, SUBJECT, number)
        _sql("""INSERT INTO chapter_notes (chapter_id, tenant_id, title, pdf_url, status)
                VALUES ($1, $2, 'Notes', 'https://example.test/n.pdf', 'published')
                ON CONFLICT (chapter_id) DO NOTHING""", node_id, TENANT)
        _sql("""INSERT INTO node_videos (node_id, youtube_id, tenant_id, title, status)
                VALUES ($1, 'vvvvvvvvvvv', $2, 'A lecture', 'published')
                ON CONFLICT DO NOTHING""", node_id, TENANT)

    _sql("""INSERT INTO nodes (node_id, tenant_id, type, title, slug, depth, status,
                               parent_id, subject_id, class_level)
            VALUES ($1, $2, 'topic', 'A topic', $1, 2, 'published', $3, $4, 11)
            ON CONFLICT (node_id) DO NOTHING""",
         LOCKED_TOPIC, TENANT, LOCKED_CHAPTER, SUBJECT)

    _sql("""INSERT INTO tests (test_id, tenant_id, title, duration_minutes, status)
            VALUES ($1, $2, 'A gate paper', 180, 'published')
            ON CONFLICT (test_id) DO NOTHING""", TEST_ID, TENANT)

    yield

    _sql("DELETE FROM test_sessions WHERE test_id = $1", TEST_ID)
    _sql("DELETE FROM tests WHERE test_id = $1", TEST_ID)
    _sql("DELETE FROM attempts WHERE firebase_uid = ANY($1)", [FREE, PRO])
    _sql("DELETE FROM study_plans WHERE firebase_uid = ANY($1)", [FREE, PRO])
    _sql("DELETE FROM entitlement_grants WHERE firebase_uid = ANY($1)", [FREE, PRO])
    _sql("DELETE FROM chapter_notes WHERE chapter_id = ANY($1)",
         [OPEN_CHAPTER, LOCKED_CHAPTER])
    _sql("DELETE FROM node_videos WHERE node_id = ANY($1)",
         [OPEN_CHAPTER, LOCKED_CHAPTER])
    _sql("DELETE FROM nodes WHERE node_id = ANY($1)",
         [LOCKED_TOPIC, OPEN_CHAPTER, LOCKED_CHAPTER, SUBJECT])
    _sql("DELETE FROM users WHERE firebase_uid = ANY($1)", [FREE, PRO])


@pytest.fixture(scope="module")
def client():
    from app.auth import current_tenant, optional_user, require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: dict(CALLER)
    app.dependency_overrides[optional_user] = (
        lambda: dict(CALLER) if CALLER.get("uid") else None
    )
    app.dependency_overrides[current_tenant] = lambda: TENANT
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def selling(monkeypatch):
    """Gates bite only where Pro can be bought, so every test here turns billing on."""
    from app.config import settings

    monkeypatch.setattr(settings, "jeene_billing_enabled", True, raising=False)
    CALLER["uid"] = FREE
    yield
    CALLER["uid"] = FREE


def as_free() -> None:
    CALLER["uid"] = FREE


def as_pro() -> None:
    CALLER["uid"] = PRO


def signed_out() -> None:
    CALLER["uid"] = None


def _question_id() -> str:
    rows = _sql("SELECT question_id FROM questions WHERE status = 'published' LIMIT 1")
    if not rows:
        pytest.skip("no published question in this database")
    return rows[0]["question_id"]


def _answer(client, attempt_id: str | None = None):
    return client.post("/attempts", json={
        "attempt_id": attempt_id or str(uuid.uuid4()),
        "question_id": _question_id(),
        "selected_option_ids": ["a"],
        "time_spent_ms": 1000,
    })


def _spend_the_allowance(uid: str, count: int) -> None:
    """Backdate nothing — just put `count` answers inside the rolling window."""
    question = _question_id()
    for _ in range(count):
        _sql("""INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id,
                                      is_correct)
                VALUES ($1::uuid, $2, $3, $4, true)""",
             str(uuid.uuid4()), uid, TENANT, question)


# --- the switch ----------------------------------------------------------------------


def test_nothing_is_locked_where_pro_cannot_be_bought(client, monkeypatch):
    """A paywall with no way through it is worse than no paywall."""
    from app.config import settings

    monkeypatch.setattr(settings, "jeene_billing_enabled", False, raising=False)
    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)
    _spend_the_allowance(FREE, gates.FREE_QUESTIONS_PER_DAY + 5)
    as_free()

    assert _answer(client).status_code == 200
    assert client.get(f"/chapters/{LOCKED_CHAPTER}/notes").status_code == 200
    assert client.post(f"/tests/{TEST_ID}/sessions").status_code == 200

    _sql("DELETE FROM test_sessions WHERE test_id = $1", TEST_ID)
    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)


# --- practice ------------------------------------------------------------------------


def test_a_free_student_gets_twenty_questions_a_day(client):
    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)
    _spend_the_allowance(FREE, gates.FREE_QUESTIONS_PER_DAY - 1)
    as_free()

    assert _answer(client).status_code == 200, "the twentieth is still free"

    response = _answer(client)
    assert response.status_code == 402
    body = response.json()["detail"]
    assert body["reason"] == gates.DAILY_PRACTICE_LIMIT
    assert body["limit"] == gates.FREE_QUESTIONS_PER_DAY
    assert body["used"] == gates.FREE_QUESTIONS_PER_DAY
    assert body["tier"] == "pro"
    assert "20 free questions" in body["message"]

    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)


def test_pro_keeps_answering_past_the_free_limit(client):
    _sql("DELETE FROM attempts WHERE firebase_uid = $1", PRO)
    _spend_the_allowance(PRO, gates.FREE_QUESTIONS_PER_DAY + 10)
    as_pro()

    assert _answer(client).status_code == 200

    _sql("DELETE FROM attempts WHERE firebase_uid = $1", PRO)


def test_retrying_an_answer_already_recorded_is_never_refused(client):
    """The client retries on a dropped response. That retry is not a new question.

    Counted naively it would be the twenty-first row and be refused — telling a student
    who is on their twentieth question that they are out, and losing the answer.
    """
    as_free()
    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)
    _spend_the_allowance(FREE, gates.FREE_QUESTIONS_PER_DAY - 1)

    attempt_id = str(uuid.uuid4())
    assert _answer(client, attempt_id).status_code == 200
    assert _answer(client, attempt_id).status_code == 200, "the same answer, sent twice"
    assert _answer(client).status_code == 402, "but a genuinely new one is refused"

    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)


# --- mock papers ---------------------------------------------------------------------


def test_a_mock_paper_needs_pro(client):
    as_free()
    response = client.post(f"/tests/{TEST_ID}/sessions")

    assert response.status_code == 402
    body = response.json()["detail"]
    assert body["reason"] == gates.PRO_ONLY
    assert "Mock papers" in body["message"]
    assert "Mistake Book" in body["message"], "it says what stays free"
    assert body["used"] is None, "not a counter, so no numbers to show"


def test_pro_can_sit_a_paper(client):
    as_pro()
    assert client.post(f"/tests/{TEST_ID}/sessions").status_code == 200
    _sql("DELETE FROM test_sessions WHERE test_id = $1", TEST_ID)


def test_a_sitting_in_progress_survives_pro_running_out(client):
    """Three hours of work must not disappear behind a paywall at the two-hour mark."""
    as_pro()
    started = client.post(f"/tests/{TEST_ID}/sessions").json()

    _sql("UPDATE users SET pro_expires_at = now() - interval '1 minute' "
         "WHERE firebase_uid = $1", PRO)

    resumed = client.post(f"/tests/{TEST_ID}/sessions")
    assert resumed.status_code == 200
    assert resumed.json()["session_id"] == started["session_id"]

    _sql("DELETE FROM test_sessions WHERE test_id = $1", TEST_ID)
    _make_pro(PRO)


# --- Jeene Mode ----------------------------------------------------------------------


def _a_plan_for(uid: str, scope: str) -> str:
    plan_id = str(uuid.uuid4())
    _sql("""INSERT INTO study_plans (plan_id, firebase_uid, tenant_id, scope_node_id,
                                     scope_type, scope_title, proficiency, intent,
                                     origin, prompt_version)
            VALUES ($1::uuid, $2, $3, $4, 'chapter', 'A chapter', 'basic', 'first_time',
                    'fallback', 1)""",
         plan_id, uid, TENANT, scope)
    return plan_id


def test_a_free_student_gets_one_plan_ever(client):
    as_free()
    _sql("DELETE FROM study_plans WHERE firebase_uid = $1", FREE)
    _a_plan_for(FREE, OPEN_CHAPTER)

    response = client.post("/plans", json={
        "scope_node_id": LOCKED_CHAPTER, "proficiency": "basic", "intent": "first_time",
    })

    assert response.status_code == 402
    body = response.json()["detail"]
    assert body["reason"] == gates.PLAN_LIMIT
    assert (body["used"], body["limit"]) == (1, gates.FREE_PLANS_EVER)

    _sql("DELETE FROM study_plans WHERE firebase_uid = $1", FREE)


def test_reopening_a_plan_already_made_is_not_a_new_plan(client):
    """Their own saved work. Telling them it is behind a paywall is the worst screen here."""
    as_free()
    _sql("DELETE FROM study_plans WHERE firebase_uid = $1", FREE)
    plan_id = _a_plan_for(FREE, OPEN_CHAPTER)

    response = client.post("/plans", json={
        "scope_node_id": OPEN_CHAPTER, "proficiency": "basic", "intent": "first_time",
    })

    assert response.status_code == 201, response.text
    assert response.json()["plan_id"] == plan_id

    _sql("DELETE FROM study_plans WHERE firebase_uid = $1", FREE)


def test_a_failed_generation_is_not_counted_against_the_allowance():
    """They got nothing, so they were not charged for it. Counted from plans, not tries."""
    async def body(conn):
        assert gates.FREE_PLANS_EVER == 1
        # `plan_generations` records attempts; `study_plans` records what arrived. The
        # gate reads the second, which is why this student still has their free plan.
        await gates.ensure_can_plan(conn, FREE, TENANT)

    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await db._init_connection(conn)
        try:
            await conn.execute("DELETE FROM study_plans WHERE firebase_uid = $1", FREE)
            await body(conn)
        finally:
            await conn.close()

    from app.config import settings
    was, settings.jeene_billing_enabled = settings.jeene_billing_enabled, True
    try:
        asyncio.run(go())
    finally:
        settings.jeene_billing_enabled = was


# --- notes and lectures ---------------------------------------------------------------


def test_the_first_chapter_of_a_subject_is_free_to_everybody(client):
    for caller in (signed_out, as_free, as_pro):
        caller()
        assert client.get(f"/chapters/{OPEN_CHAPTER}/notes").status_code == 200
        assert client.get(f"/chapters/{OPEN_CHAPTER}/videos").status_code == 200
        assert client.get(f"/nodes/{OPEN_CHAPTER}/video-groups").status_code == 200


def test_a_later_chapter_needs_pro(client):
    for caller in (signed_out, as_free):
        caller()
        response = client.get(f"/chapters/{LOCKED_CHAPTER}/notes")
        assert response.status_code == 402
        body = response.json()["detail"]
        assert body["reason"] == gates.CHAPTER_LOCKED
        assert body["message"].startswith("Notes for this chapter")
        assert "first chapter of each subject is free" in body["message"]


def test_pro_reads_every_chapter(client):
    as_pro()
    assert client.get(f"/chapters/{LOCKED_CHAPTER}/notes").status_code == 200
    assert client.get(f"/nodes/{LOCKED_CHAPTER}/video-groups").status_code == 200


def test_the_lock_follows_the_chapter_a_topic_hangs_under(client):
    """The video screens are opened from topics and concepts, not only from chapters."""
    as_free()
    response = client.get(f"/nodes/{LOCKED_TOPIC}/video-groups")
    assert response.status_code == 402
    assert response.json()["detail"]["message"].startswith("Video lectures")

    as_pro()
    assert client.get(f"/nodes/{LOCKED_TOPIC}/video-groups").status_code == 200


def test_the_mistake_book_and_the_report_stay_free(client):
    """The two things that make a free account feel owned rather than borrowed."""
    as_free()
    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)
    _spend_the_allowance(FREE, gates.FREE_QUESTIONS_PER_DAY + 5)

    assert client.get("/mistakes").status_code == 200
    assert client.get("/progress").status_code == 200

    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)


def test_one_answer_id_cannot_be_reused_to_read_the_question_bank(client):
    """The way round the daily cap, if an id were allowed to wander between questions.

    Nothing is written for a duplicate id, so the allowance never moves — and the reply
    to `/attempts` contains the correct options and the worked solution. A client that
    sent one id for every question would have had the whole bank for free while the
    counter stayed at zero.
    """
    as_free()
    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)

    questions = _sql(
        "SELECT question_id FROM questions WHERE status = 'published' LIMIT 2")
    if len(questions) < 2:
        pytest.skip("needs two published questions")
    first, second = (q["question_id"] for q in questions)

    reused = str(uuid.uuid4())
    assert client.post("/attempts", json={
        "attempt_id": reused, "question_id": first,
        "selected_option_ids": ["a"], "time_spent_ms": 1000,
    }).status_code == 200

    # Same id, different question. This is the whole exploit.
    response = client.post("/attempts", json={
        "attempt_id": reused, "question_id": second,
        "selected_option_ids": ["a"], "time_spent_ms": 1000,
    })

    assert response.status_code == 409
    assert "correct_option_ids" not in response.text
    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)


def test_an_answer_id_belonging_to_somebody_else_is_refused(client):
    """The same check, from the other direction: ids are the client's to choose."""
    as_pro()
    _sql("DELETE FROM attempts WHERE firebase_uid = ANY($1)", [FREE, PRO])
    question = _question_id()
    theirs = str(uuid.uuid4())
    _sql("""INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id,
                                  is_correct)
            VALUES ($1::uuid, $2, $3, $4, true)""", theirs, FREE, TENANT, question)

    response = client.post("/attempts", json={
        "attempt_id": theirs, "question_id": question,
        "selected_option_ids": ["a"], "time_spent_ms": 1000,
    })

    assert response.status_code == 409
    _sql("DELETE FROM attempts WHERE firebase_uid = ANY($1)", [FREE, PRO])


def test_a_genuine_retry_of_the_same_answer_still_works(client):
    """The reason the id exists at all: a dropped response must be resendable."""
    as_free()
    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)
    payload = {
        "attempt_id": str(uuid.uuid4()), "question_id": _question_id(),
        "selected_option_ids": ["a"], "time_spent_ms": 1000,
    }

    first = client.post("/attempts", json=payload)
    second = client.post("/attempts", json=payload)

    assert (first.status_code, second.status_code) == (200, 200)
    assert second.json()["already_recorded"] is True
    assert len(_sql("SELECT 1 FROM attempts WHERE firebase_uid = $1", FREE)) == 1
    _sql("DELETE FROM attempts WHERE firebase_uid = $1", FREE)


def test_an_answer_cannot_be_read_without_signing_in(client):
    """The hole that made the whole free tier optional.

    Browsing the syllabus and reading questions is anonymous on purpose — somebody
    deciding whether to make an account should see what is in here. The *answer* was too,
    and the daily allowance is counted from `attempts`, which needs a token. So signing in
    was what switched the limit on, and staying signed out bought unlimited practice with
    full worked solutions. Every reason to pay evaporated for anyone who never signed in.
    """
    from app.auth import current_tenant, optional_user, require_user
    from app.main import app

    question = _question_id()
    # A caller with no token at all, which is what `require_user` is given in production.
    app.dependency_overrides.pop(require_user, None)
    app.dependency_overrides[optional_user] = lambda: None
    try:
        answer = client.get(f"/questions/{question}/answer")
        explanation = client.get(f"/questions/{question}/explanation")

        assert answer.status_code == 401, answer.text
        assert "correct_option_ids" not in answer.text
        assert explanation.status_code == 401
    finally:
        app.dependency_overrides[require_user] = lambda: dict(CALLER)
        app.dependency_overrides[optional_user] = (
            lambda: dict(CALLER) if CALLER.get("uid") else None
        )
        app.dependency_overrides[current_tenant] = lambda: TENANT


def test_browsing_the_questions_themselves_is_still_free(client):
    """The half that must stay open. A locked catalogue converts nobody."""
    from app.auth import optional_user, require_user
    from app.main import app

    app.dependency_overrides.pop(require_user, None)
    app.dependency_overrides[optional_user] = lambda: None
    try:
        assert client.get("/chapters").status_code == 200
        assert client.get(f"/questions/{_question_id()}").status_code == 200
    finally:
        app.dependency_overrides[require_user] = lambda: dict(CALLER)
        app.dependency_overrides[optional_user] = (
            lambda: dict(CALLER) if CALLER.get("uid") else None
        )
