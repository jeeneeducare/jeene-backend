"""Asking Jeene a doubt, end to end.

The ordering inside `/doubts/ask` is most of what these check: the anchor before the gate,
the gate before the model, the record after it. Any other order either spends a student's
doubt on an answer they never got, or answers from material nobody checked.
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
from app.doubts.schema import DoubtAnswer
from app.providers.base import ProviderError, ProviderUsage

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="these write threads and messages"
)

TENANT = "JEENE_MASTER"
FREE = "test-doubt-free"
PRO = "test-doubt-pro"

SUBJECT = "test_doubt_subject"
CHAPTER = "test_doubt_chapter"
OTHER_CHAPTER = "test_doubt_chapter_2"
BARE_CHAPTER = "test_doubt_chapter_bare"  # published, and nothing under it
TOPIC = "test_doubt_topic"
CONCEPT = "test_doubt_concept"
OTHER_CONCEPT = "test_doubt_concept_2"
QUESTION = "test_doubt_question"

CALLER: dict = {"uid": PRO}


def _sql(statement, *args):
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await db._init_connection(conn)
        try:
            return await conn.fetch(statement, *args)
        finally:
            await conn.close()
    return asyncio.run(go())


class FakeProvider:
    """Answers with whatever it was handed, and remembers what it was asked."""

    name = "fake"
    model = "fake-1"

    def __init__(self):
        self.reply: DoubtAnswer | Exception = DoubtAnswer(
            answer="Because the mass cancels.",
            answered=True,
            used_concept_ids=[CONCEPT],
            used_question_ids=[],
            used_notes=False,
        )
        self.calls: list[dict] = []

    async def read_json(self, system_prompt, context, user_text, schema,
                        max_output_tokens=None):
        self.calls.append({"context": context, "user_text": user_text})
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply, ProviderUsage(
            provider=self.name, model=self.model, input_tokens=100, output_tokens=20
        )


@pytest.fixture(scope="module", autouse=True)
def world():
    """Two students, and a chapter with something in it to answer from."""
    for uid in (FREE, PRO):
        _sql("""INSERT INTO users (firebase_uid, tenant_id) VALUES ($1, $2)
                ON CONFLICT (firebase_uid) DO NOTHING""", uid, TENANT)

    async def make_pro():
        from app.billing import entitlements
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await db._init_connection(conn)
        try:
            await entitlements.grant(conn, uid=PRO, tenant=TENANT, days=30,
                                     provider="test", note="doubt tests")
        finally:
            await conn.close()
    asyncio.run(make_pro())

    _sql("""INSERT INTO nodes (node_id, tenant_id, type, title, slug, depth, status,
                               subject_id)
            VALUES ($1, $2, 'subject', 'Doubt Subject', $1, 0, 'published', $1)
            ON CONFLICT (node_id) DO NOTHING""", SUBJECT, TENANT)
    for node_id, title in ((CHAPTER, "A Chapter"), (OTHER_CHAPTER, "Another Chapter"),
                           (BARE_CHAPTER, "An Empty Chapter")):
        _sql("""INSERT INTO nodes (node_id, tenant_id, type, title, slug, depth, status,
                                   parent_id, subject_id)
                VALUES ($1, $2, 'chapter', $3, $1, 1, 'published', $4, $5)
                ON CONFLICT (node_id) DO NOTHING""",
             node_id, TENANT, title, SUBJECT, SUBJECT)
    _sql("""INSERT INTO nodes (node_id, tenant_id, type, title, slug, depth, status,
                               parent_id, subject_id)
            VALUES ($1, $2, 'topic', 'A Topic', $1, 2, 'published', $3, $4)
            ON CONFLICT (node_id) DO NOTHING""", TOPIC, TENANT, CHAPTER, SUBJECT)
    _sql("""INSERT INTO nodes (node_id, tenant_id, type, title, slug, depth, status,
                               parent_id, subject_id, description)
            VALUES ($1, $2, 'concept', 'Free Fall', $1, 3, 'published', $3, $4,
                    'Everything falls at the same rate.')
            ON CONFLICT (node_id) DO NOTHING""", CONCEPT, TENANT, TOPIC, SUBJECT)
    # The second chapter needs something to answer from too, or asking about it is
    # refused before the model and never reaches the thread it is meant to test.
    _sql("""INSERT INTO nodes (node_id, tenant_id, type, title, slug, depth, status,
                               parent_id, subject_id, description)
            VALUES ($1, $2, 'concept', 'Something Else', $1, 2, 'published', $3, $4,
                    'A second chapter needs a second idea in it.')
            ON CONFLICT (node_id) DO NOTHING""",
         OTHER_CONCEPT, TENANT, OTHER_CHAPTER, SUBJECT)
    _sql("""INSERT INTO questions (question_id, tenant_id, question_type, question_text,
                                   options_json, correct_option_ids, explanation_json,
                                   status)
            VALUES ($1, $2, 'mcq', 'Which falls faster?', $3::jsonb, $4, $5::jsonb,
                    'published')
            ON CONFLICT (question_id) DO NOTHING""",
         QUESTION, TENANT,
         '[{"option_id": "a", "text": "the heavy one"}, {"option_id": "b", "text": "same"}]',
         ["b"], '{"text": "The mass cancels.", "format": "markdown"}')
    _sql("""INSERT INTO question_concept_mappings (question_id, concept_node_id, is_primary)
            VALUES ($1, $2, TRUE) ON CONFLICT DO NOTHING""", QUESTION, CONCEPT)

    yield

    _sql("DELETE FROM doubt_messages WHERE firebase_uid = ANY($1)", [FREE, PRO])
    _sql("DELETE FROM doubt_threads WHERE firebase_uid = ANY($1)", [FREE, PRO])
    _sql("DELETE FROM entitlement_grants WHERE firebase_uid = ANY($1)", [FREE, PRO])
    _sql("DELETE FROM question_concept_mappings WHERE question_id = $1", QUESTION)
    _sql("DELETE FROM questions WHERE question_id = $1", QUESTION)
    _sql("DELETE FROM nodes WHERE node_id = ANY($1)",
         [CONCEPT, OTHER_CONCEPT, TOPIC, CHAPTER, OTHER_CHAPTER, BARE_CHAPTER,
          SUBJECT])
    _sql("DELETE FROM users WHERE firebase_uid = ANY($1)", [FREE, PRO])


@pytest.fixture(scope="module")
def client():
    from app.auth import current_tenant, require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: dict(CALLER)
    app.dependency_overrides[current_tenant] = lambda: TENANT
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Billing on, Pro caller, no doubts asked yet, and a provider that does not dial out."""
    from app.config import settings
    from app.routers import doubts as router_module

    monkeypatch.setattr(settings, "jeene_billing_enabled", True, raising=False)
    CALLER["uid"] = PRO
    _sql("DELETE FROM doubt_messages WHERE firebase_uid = ANY($1)", [FREE, PRO])
    _sql("DELETE FROM doubt_threads WHERE firebase_uid = ANY($1)", [FREE, PRO])

    provider = FakeProvider()
    monkeypatch.setattr(router_module, "build_doubt_provider", lambda: provider)
    yield provider
    CALLER["uid"] = PRO


def ask(client, text="why do things fall", kind="node", anchor=CHAPTER):
    return client.post("/doubts/ask", json={
        "anchor_kind": kind, "anchor_id": anchor, "text": text,
    })


def _spend(uid: str, count: int, thread: str | None = None) -> None:
    """Put `count` asked doubts inside the rolling window, without a model call."""
    thread_id = thread or _sql(
        """INSERT INTO doubt_threads (firebase_uid, tenant_id, chapter_id)
           VALUES ($1, $2, $3)
           ON CONFLICT (firebase_uid, chapter_id) DO UPDATE SET last_message_at = now()
           RETURNING thread_id""", uid, TENANT, CHAPTER)[0]["thread_id"]
    for _ in range(count):
        _sql("""INSERT INTO doubt_messages (thread_id, firebase_uid, role, text)
                VALUES ($1, $2, 'student', 'spent')""", thread_id, uid)


# --- the gate ---------------------------------------------------------------------------


def test_ask_jeene_is_for_pro(client):
    CALLER["uid"] = FREE
    response = ask(client)

    assert response.status_code == 402
    assert response.json()["detail"]["reason"] == gates.PRO_ONLY


def test_ten_a_day_and_then_not(client):
    _spend(PRO, gates.PRO_DOUBTS_PER_DAY)
    response = ask(client)

    assert response.status_code == 402
    body = response.json()["detail"]
    assert body["reason"] == gates.DAILY_DOUBT_LIMIT
    assert body["used"] == gates.PRO_DOUBTS_PER_DAY
    assert body["limit"] == gates.PRO_DOUBTS_PER_DAY


def test_the_daily_cap_holds_even_with_billing_switched_off(client, monkeypatch):
    """The one place a gate here ignores the billing flag, on purpose.

    Whether Ask Jeene sits behind Pro is a monetisation question and turns off with
    billing. How many a student may ask is not: every doubt is a model call billed to a
    real account, and a deployment with billing off is exactly the one where nobody is
    watching that bill.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "jeene_billing_enabled", False, raising=False)
    CALLER["uid"] = FREE

    assert ask(client).status_code == 200, "billing off, so no Pro wall"

    _spend(FREE, gates.PRO_DOUBTS_PER_DAY)
    refused = ask(client)
    assert refused.status_code == 402
    assert refused.json()["detail"]["reason"] == gates.DAILY_DOUBT_LIMIT


def test_a_refused_doubt_is_never_charged_for(client, fresh):
    """The gate runs before the model and the record after it, so there is no ordering
    where a student pays for an answer they did not get."""
    CALLER["uid"] = FREE
    ask(client)

    assert fresh.calls == [], "no model call for a student who was refused"
    assert _sql("SELECT count(*) AS n FROM doubt_messages WHERE firebase_uid = $1",
                FREE)[0]["n"] == 0


def test_what_is_left_today_comes_back_with_the_answer(client):
    first = ask(client).json()
    assert first["doubts_left_today"] == gates.PRO_DOUBTS_PER_DAY - 1

    second = ask(client).json()
    assert second["doubts_left_today"] == gates.PRO_DOUBTS_PER_DAY - 2


# --- the anchor -------------------------------------------------------------------------


def test_an_anchor_that_does_not_resolve_is_not_worth_a_model_call(client, fresh):
    response = ask(client, anchor="no_such_node")

    assert response.status_code == 404
    assert fresh.calls == []


def test_a_chapter_with_nothing_in_it_is_refused_before_the_model(client, fresh):
    """An empty material block would produce exactly the confident invention this whole
    feature exists to prevent, so it is refused before the call rather than after."""
    response = ask(client, anchor=BARE_CHAPTER)

    assert response.status_code == 422
    assert fresh.calls == []


def test_a_question_anchor_answers_from_its_chapter(client):
    response = ask(client, kind="question", anchor=QUESTION)

    assert response.status_code == 200
    thread = client.get(f"/doubts/chapters/{CHAPTER}").json()
    assert len(thread["messages"]) == 2


# --- the conversation -------------------------------------------------------------------


def test_the_question_and_the_answer_are_stored_together(client):
    response = ask(client, text="why do heavy things not fall faster")

    assert response.status_code == 200
    body = response.json()
    assert body["message"]["role"] == "jeene"
    assert body["message"]["text"] == "Because the mass cancels."
    assert body["message"]["answered"] is True

    messages = client.get(f"/doubts/chapters/{CHAPTER}").json()["messages"]
    assert [m["role"] for m in messages] == ["student", "jeene"]
    assert messages[0]["text"] == "why do heavy things not fall faster"


def test_one_chapter_is_one_conversation(client):
    first = ask(client, text="one").json()["thread_id"]
    second = ask(client, text="two").json()["thread_id"]

    assert first == second
    assert len(client.get(f"/doubts/chapters/{CHAPTER}").json()["messages"]) == 4


def test_a_different_chapter_is_a_different_conversation(client):
    here = ask(client, anchor=CHAPTER).json()["thread_id"]
    there = ask(client, anchor=OTHER_CHAPTER).json()["thread_id"]

    assert here != there


def test_the_conversation_so_far_goes_back_with_the_next_doubt(client, fresh):
    """A thread runs for a whole chapter, and "but why?" only means something next to
    what it follows."""
    ask(client, text="what is free fall")
    ask(client, text="but why?")

    assert "what is free fall" in fresh.calls[-1]["context"]
    assert "Because the mass cancels." in fresh.calls[-1]["context"]


def test_a_chapter_never_asked_about_is_an_empty_thread_not_a_missing_one(client):
    response = client.get(f"/doubts/chapters/{OTHER_CHAPTER}")

    assert response.status_code == 200
    assert response.json()["messages"] == []
    assert response.json()["chapter_title"] == "Another Chapter"


def test_a_chapter_that_does_not_exist_is_missing(client):
    assert client.get("/doubts/chapters/no_such_chapter").status_code == 404


def test_reading_a_thread_back_does_not_spend_a_doubt(client):
    ask(client)
    before = client.get(f"/doubts/chapters/{CHAPTER}").json()["doubts_left_today"]
    client.get(f"/doubts/chapters/{CHAPTER}")
    after = client.get(f"/doubts/chapters/{CHAPTER}").json()["doubts_left_today"]

    assert before == after


def test_one_students_thread_is_not_anothers(client):
    ask(client, text="mine")
    CALLER["uid"] = FREE

    assert client.get(f"/doubts/chapters/{CHAPTER}").json()["messages"] == []


# --- when it goes wrong ------------------------------------------------------------------


def test_a_provider_failure_stores_nothing(client, fresh):
    """Different from a refusal: the question was fine. Storing half an exchange would
    show as Jeene ignoring them, and would still have cost them a doubt."""
    fresh.reply = ProviderError("upstream fell over")
    response = ask(client)

    assert response.status_code == 503
    assert _sql("SELECT count(*) AS n FROM doubt_messages WHERE firebase_uid = $1",
                PRO)[0]["n"] == 0


def test_an_ungrounded_answer_is_stored_as_the_refusal_the_student_saw(client, fresh):
    """What was shown is what is recorded. A record that kept the model's discarded prose
    would be a log of things no student was ever told."""
    from app.doubts.answer import COULD_NOT_ANSWER

    fresh.reply = DoubtAnswer(
        answer="Here is something I made up.", answered=True,
        used_concept_ids=["c_invented"], used_question_ids=[], used_notes=False,
    )
    body = ask(client).json()

    assert body["message"]["text"] == COULD_NOT_ANSWER
    assert body["message"]["answered"] is False
    stored = client.get(f"/doubts/chapters/{CHAPTER}").json()["messages"][1]
    assert stored["text"] == COULD_NOT_ANSWER


def test_ask_jeene_says_so_when_it_is_not_configured(client, monkeypatch):
    from app.routers import doubts as router_module
    monkeypatch.setattr(router_module, "build_doubt_provider", lambda: None)

    assert ask(client).status_code == 503


def test_an_essay_is_refused_rather_than_quietly_cut_in_half(client, fresh):
    """A truncated question gets a confident answer to half of it, which is worse than
    being asked to shorten it."""
    response = ask(client, text="x" * 1001)

    assert response.status_code == 422
    assert fresh.calls == []


def test_an_empty_doubt_is_not_a_doubt(client, fresh):
    """A stray tap must not cost one of the ten."""
    assert ask(client, text="   ").status_code == 422
    assert ask(client, text="").status_code == 422
    assert fresh.calls == []


def test_the_question_comes_before_its_answer(client):
    """Both halves of an exchange are written in one transaction, where `now()` is the
    transaction's start — so with the column's original default they carried the same
    instant and the thread fell back to ordering by a random uuid. An answer rendering
    above the question it answers is not a subtle failure.

    Eight exchanges, because one would pass by coin-flip.
    """
    for i in range(8):
        assert ask(client, text=f"doubt {i}").status_code == 200

    messages = client.get(f"/doubts/chapters/{CHAPTER}").json()["messages"]
    assert [m["role"] for m in messages] == ["student", "jeene"] * 8
    assert [m["text"] for m in messages][::2] == [f"doubt {i}" for i in range(8)]


# --- this was wrong ----------------------------------------------------------------------


def test_a_student_can_say_an_answer_was_wrong(client):
    message_id = ask(client).json()["message"]["message_id"]

    assert client.post(f"/doubts/messages/{message_id}/report").status_code == 204

    stored = client.get(f"/doubts/chapters/{CHAPTER}").json()["messages"][1]
    assert stored["reported"] is True


def test_reporting_the_same_answer_twice_is_not_an_error(client):
    message_id = ask(client).json()["message"]["message_id"]
    client.post(f"/doubts/messages/{message_id}/report")

    assert client.post(f"/doubts/messages/{message_id}/report").status_code == 204


def test_nobody_reports_somebody_elses_answer(client):
    message_id = ask(client).json()["message"]["message_id"]
    CALLER["uid"] = FREE

    assert client.post(f"/doubts/messages/{message_id}/report").status_code == 404


def test_a_student_cannot_report_their_own_question(client):
    ask(client)
    student_message = client.get(f"/doubts/chapters/{CHAPTER}").json()["messages"][0]

    assert client.post(
        f"/doubts/messages/{student_message['message_id']}/report"
    ).status_code == 404


def test_reporting_something_that_does_not_exist_is_missing(client):
    assert client.post(
        f"/doubts/messages/{uuid.uuid4()}/report"
    ).status_code == 404


# --- watching it ---------------------------------------------------------------------


def test_the_health_figures_say_what_ask_jeene_has_been_doing(client, fresh, monkeypatch):
    """The numbers the design document said to watch.

    `refusal_rate` near zero means it is answering past what the material supports; very
    high means the material is too thin to be worth asking. `ungrounded` is different in
    kind — it is answers thrown away for citing material they were never given, and it is
    a bug rather than a signal, so it must read zero.
    """
    from app.config import settings
    monkeypatch.setattr(settings, "jeene_reconcile_secret", "s3cret", raising=False)

    ask(client)                                     # answered
    fresh.reply = DoubtAnswer(
        answer="That belongs to another chapter.", answered=False,
        used_concept_ids=[], used_question_ids=[], used_notes=False)
    ask(client)                                     # the model declining
    fresh.reply = DoubtAnswer(
        answer="Invented.", answered=True,
        used_concept_ids=["C99"], used_question_ids=[], used_notes=False)
    ask(client)                                     # thrown away by the check

    body = client.get("/internal/doubts/health",
                      headers={"X-Jeene-Reconcile": "s3cret"}).json()

    assert body["asked"] == 3
    assert body["answered"] == 1
    assert body["declined"] == 2, "the model's refusal and the dropped answer"
    assert body["ungrounded"] == 1, "only the one we threw away"
    assert body["refusal_rate"] == round(2 / 3, 3)
    assert body["students"] == 1
    assert body["tokens_in"] > 0
    assert any("A Chapter" in c for c in body["busiest_chapters"])


def test_nobody_reads_the_health_figures_without_the_secret(client, monkeypatch):
    """Nothing here returns what a student wrote, but the shape of somebody's week is not
    public either — and an unset secret is a refusal rather than a way in."""
    from app.config import settings

    monkeypatch.setattr(settings, "jeene_reconcile_secret", "s3cret", raising=False)
    assert client.get("/internal/doubts/health").status_code == 403
    assert client.get("/internal/doubts/health",
                      headers={"X-Jeene-Reconcile": "wrong"}).status_code == 403

    monkeypatch.setattr(settings, "jeene_reconcile_secret", None, raising=False)
    assert client.get("/internal/doubts/health",
                      headers={"X-Jeene-Reconcile": "s3cret"}).status_code == 503


# --- the wall arrives before the typing ------------------------------------------------


def test_a_free_student_is_told_before_they_write_their_question(client):
    """Found by audit: the counter knows nothing about Pro.

    A free student opened the sheet, was told "10 left today", wrote out their doubt,
    tapped send, and only then learned the feature was not theirs. The thread now carries
    the wall they would hit, so the sheet can draw it before the box is ever live.
    """
    CALLER["uid"] = FREE
    body = client.get(f"/doubts/chapters/{CHAPTER}").json()

    assert body["gate"] is not None
    assert body["gate"]["reason"] == gates.PRO_ONLY


def test_a_pro_student_with_doubts_left_meets_no_wall(client):
    body = client.get(f"/doubts/chapters/{CHAPTER}").json()

    assert body["gate"] is None
    assert body["doubts_left_today"] == gates.PRO_DOUBTS_PER_DAY


def test_a_pro_student_out_of_doubts_is_told_on_opening(client):
    _spend(PRO, gates.PRO_DOUBTS_PER_DAY)
    body = client.get(f"/doubts/chapters/{CHAPTER}").json()

    assert body["gate"]["reason"] == gates.DAILY_DOUBT_LIMIT
    assert body["doubts_left_today"] == 0


def test_a_thread_does_not_grow_without_limit(client):
    """A student at ten a day who keeps returning to one chapter accumulates for ever,
    and an answer is around 1,500 characters — an unbounded read is a few hundred
    kilobytes of JSON on mobile data every time the sheet opens."""
    from app.doubts import store as doubt_store

    thread_id = _sql(
        """INSERT INTO doubt_threads (firebase_uid, tenant_id, chapter_id)
           VALUES ($1,$2,$3) ON CONFLICT (firebase_uid, chapter_id)
           DO UPDATE SET last_message_at = now() RETURNING thread_id""",
        PRO, TENANT, CHAPTER)[0]["thread_id"]
    for i in range(doubt_store.MAX_THREAD_MESSAGES + 12):
        _sql("""INSERT INTO doubt_messages (thread_id, firebase_uid, role, text)
                VALUES ($1,$2,'student',$3)""", thread_id, PRO, f"m{i}")

    messages = client.get(f"/doubts/chapters/{CHAPTER}").json()["messages"]

    assert len(messages) == doubt_store.MAX_THREAD_MESSAGES
    # The oldest are what gets dropped. Truncating the other end would open the sheet on
    # something said months ago with the latest answer missing.
    assert messages[-1]["text"] == f"m{doubt_store.MAX_THREAD_MESSAGES + 11}"
    assert messages[0]["text"] == "m12"


def test_the_reply_says_which_chapter_it_joined(client):
    """The server decides which thread an anchor belongs to, not the app.

    A question resolves to the chapter of its primary concept, and that is where the
    exchange is written. A surface that does not know its chapter — a plan step whose
    scope is a topic — can therefore still ask, and the sheet corrects itself from this
    rather than showing one thread while writing to another.
    """
    body = ask(client, kind="question", anchor=QUESTION).json()

    assert body["chapter_id"] == CHAPTER
    assert body["chapter_title"] == "A Chapter"
