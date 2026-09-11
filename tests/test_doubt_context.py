"""What Jeene is allowed to read before it answers.

The phase worth being slow about. If the right material is chosen a plain prompt gives
good answers; if the wrong material is chosen no prompt can save it — so what is asserted
here is not that the code runs but that it picks the right things and, just as important,
refuses to reach outside the scope the student is standing in.

A small synthetic chapter rather than the seeded content, because these assertions are
about ordering and exclusion and both need material whose shape is known exactly.
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid

import asyncpg
import pytest

from app import db
from app.doubts import context

pytestmark = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"), reason="the material lives in the database"
)

TENANT = "JEENE_MASTER"


def in_tx(body):
    async def go():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await db._init_connection(conn)
        tx = conn.transaction()
        await tx.start()
        try:
            return await body(conn)
        finally:
            await tx.rollback()
            await conn.close()
    return asyncio.run(go())


class World:
    """One chapter, two topics, four concepts, a question on each."""

    def __init__(self, tag: str):
        self.chapter = f"dc_ch_{tag}"
        self.subject = f"dc_sub_{tag}"
        self.topic_a = f"dc_ta_{tag}"
        self.topic_b = f"dc_tb_{tag}"
        self.concepts = {
            "escape": (f"dc_c1_{tag}", "Escape Speed",
                       "The least speed at which a body leaves a planet for good."),
            "orbit": (f"dc_c2_{tag}", "Orbital Motion",
                      "A satellite falling around a planet rather than into it."),
            "friction": (f"dc_c3_{tag}", "Kinetic Friction",
                         "The force opposing two surfaces already sliding."),
            "normal": (f"dc_c4_{tag}", "Normal Reaction",
                       "The push a surface returns along its own perpendicular."),
        }
        self.questions = {k: f"dc_q_{k}_{tag}" for k in self.concepts}


async def build(conn, w: World) -> None:
    async def node(node_id, kind, title, parent, depth, description=None, order=0):
        await conn.execute(
            """INSERT INTO nodes (node_id, tenant_id, type, title, slug, depth, status,
                                  parent_id, subject_id, description, display_order)
               VALUES ($1,$2,$3,$4,$1,$5,'published',$6,$7,$8,$9)""",
            node_id, TENANT, kind, title, depth, parent, w.subject, description, order)

    await node(w.subject, "subject", "Doubt Subject", None, 0)
    await node(w.chapter, "chapter", "A Test Chapter", w.subject, 1)
    await node(w.topic_a, "topic", "Gravity things", w.chapter, 2, order=1)
    await node(w.topic_b, "topic", "Contact forces", w.chapter, 2, order=2)

    placement = {"escape": (w.topic_a, 1), "orbit": (w.topic_a, 2),
                 "friction": (w.topic_b, 1), "normal": (w.topic_b, 2)}
    for key, (node_id, title, description) in w.concepts.items():
        parent, order = placement[key]
        await node(node_id, "concept", title, parent, 3, description, order)

        qid = w.questions[key]
        await conn.execute(
            """INSERT INTO questions (question_id, tenant_id, question_type, question_text,
                                      options_json, correct_option_ids, explanation_json,
                                      status)
               VALUES ($1,$2,'mcq',$3,$4::jsonb,$5,$6::jsonb,'published')""",
            qid, TENANT, f"A question about {title}",
            json.dumps([{"option_id": "a", "text": "yes"}, {"option_id": "b", "text": "no"}]),
            ["a"], json.dumps({"text": f"Because of {title}.", "format": "markdown"}))
        await conn.execute(
            """INSERT INTO question_concept_mappings (question_id, concept_node_id, is_primary)
               VALUES ($1,$2,TRUE)""", qid, node_id)


def world(body):
    def run(conn):
        async def inner():
            w = World(uuid.uuid4().hex[:8])
            await build(conn, w)
            return await body(conn, w)
        return inner()
    return in_tx(run)


# --- resolving where they asked from ------------------------------------------------


def test_every_anchor_resolves_to_the_chapter_its_thread_belongs_to():
    async def body(conn, w):
        for kind, anchor_id in (("node", w.chapter), ("node", w.topic_a),
                                ("node", w.concepts["escape"][0]),
                                ("question", w.questions["escape"])):
            a = await context.resolve(conn, kind, anchor_id, TENANT)
            assert a is not None, f"{kind}:{anchor_id}"
            assert a.chapter_id == w.chapter
    world(body)


def test_a_subject_is_not_something_a_doubt_can_hang_on():
    """Nothing above a chapter has a thread, and gathering a whole subject would be a
    haystack rather than grounding."""
    async def body(conn, w):
        assert await context.resolve(conn, "node", w.subject, TENANT) is None
    world(body)


def test_an_anchor_that_does_not_exist_is_refused_rather_than_guessed():
    async def body(conn, w):
        assert await context.resolve(conn, "node", "no_such_node", TENANT) is None
        assert await context.resolve(conn, "question", "no_such_q", TENANT) is None
        assert await context.resolve(conn, "notes", w.chapter, TENANT) is None
        assert await context.resolve(conn, "nonsense", w.chapter, TENANT) is None
    world(body)


# --- what gets gathered ---------------------------------------------------------------


def test_a_topic_anchor_gathers_only_what_is_under_it():
    """The scope is the fence. A doubt asked on gravity must not be answered out of
    friction, however the student happened to word it."""
    async def body(conn, w):
        a = await context.resolve(conn, "node", w.topic_a, TENANT)
        m = await context.gather(conn, a, "friction and normal reaction", "nobody", TENANT)

        titles = {c.title for c in m.concepts}
        assert titles == {"Escape Speed", "Orbital Motion"}
        assert "Kinetic Friction" not in titles, "words must rank, never widen"
    world(body)


def test_a_chapter_anchor_gathers_the_whole_chapter():
    async def body(conn, w):
        a = await context.resolve(conn, "node", w.chapter, TENANT)
        m = await context.gather(conn, a, "anything", "nobody", TENANT)
        assert len(m.concepts) == 4
        assert len(m.solutions) == 4
    world(body)


def test_the_concept_asked_about_is_read_first():
    async def body(conn, w):
        a = await context.resolve(conn, "node", w.chapter, TENANT)

        escape = await context.gather(conn, a, "what is escape speed", "nobody", TENANT)
        friction = await context.gather(conn, a, "how does kinetic friction work",
                                        "nobody", TENANT)

        assert escape.concepts[0].title == "Escape Speed"
        assert friction.concepts[0].title == "Kinetic Friction"
    world(body)


def test_the_ranking_decides_which_solutions_come_back():
    """The bug this test exists for, found before it ever shipped.

    `= ANY($array)` matches without caring about order, so the first version scored every
    concept, handed them over ranked, and then let Postgres return whichever rows it
    reached first. Asking about escape speed came back with the same worked solutions as
    asking nothing at all — the ranking ran and was thrown away.
    """
    async def body(conn, w):
        a = await context.resolve(conn, "node", w.chapter, TENANT)
        monkeyed = context.MAX_SOLUTIONS
        context.MAX_SOLUTIONS = 1
        try:
            escape = await context.gather(conn, a, "escape speed", "nobody", TENANT)
            friction = await context.gather(conn, a, "kinetic friction", "nobody", TENANT)
        finally:
            context.MAX_SOLUTIONS = monkeyed

        assert escape.solutions[0].question_id == w.questions["escape"]
        assert friction.solutions[0].question_id == w.questions["friction"]
    world(body)


def test_a_question_with_no_useful_words_falls_back_to_teaching_order():
    """"I don't understand this" is the commonest doubt there is, and it carries no
    signal. The chapter's own order is the right answer, not a random one."""
    async def body(conn, w):
        a = await context.resolve(conn, "node", w.chapter, TENANT)
        m = await context.gather(conn, a, "i don't understand this", "nobody", TENANT)
        assert [c.title for c in m.concepts] == [
            "Escape Speed", "Orbital Motion", "Kinetic Friction", "Normal Reaction"]
    world(body)


def test_a_question_anchor_carries_the_question_and_does_not_repeat_it():
    async def body(conn, w):
        a = await context.resolve(conn, "question", w.questions["escape"], TENANT)
        m = await context.gather(conn, a, "why is this the answer", "nobody", TENANT)

        assert m.focus is not None
        assert m.focus.question_id == w.questions["escape"]
        assert m.focus.correct == "a"
        assert "Escape Speed" in m.focus.explanation
        assert w.questions["escape"] not in {s.question_id for s in m.solutions}, \
            "the focus is not also one of the examples"
    world(body)


def test_what_the_student_just_got_wrong_comes_with_the_question():
    """The difference between a tutor and a search box: it can see they picked (b) on
    this exact question four minutes ago."""
    async def body(conn, w):
        uid = f"dc-{uuid.uuid4()}"
        await conn.execute(
            "INSERT INTO users (firebase_uid, tenant_id) VALUES ($1,$2)", uid, TENANT)
        await conn.execute(
            """INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id,
                                     is_correct)
               VALUES (gen_random_uuid(), $1, $2, $3, FALSE)""",
            uid, TENANT, w.questions["escape"])

        a = await context.resolve(conn, "node", w.chapter, TENANT)
        m = await context.gather(conn, a, "escape speed", uid, TENANT)

        assert [r.question_id for r in m.recent] == [w.questions["escape"]]
        assert m.recent[0].was_correct is False
    world(body)


def test_one_students_attempts_never_appear_in_anothers_context():
    async def body(conn, w):
        mine = f"dc-{uuid.uuid4()}"
        theirs = f"dc-{uuid.uuid4()}"
        for uid in (mine, theirs):
            await conn.execute(
                "INSERT INTO users (firebase_uid, tenant_id) VALUES ($1,$2)", uid, TENANT)
        await conn.execute(
            """INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id,
                                     is_correct)
               VALUES (gen_random_uuid(), $1, $2, $3, FALSE)""",
            theirs, TENANT, w.questions["escape"])

        a = await context.resolve(conn, "node", w.chapter, TENANT)
        m = await context.gather(conn, a, "escape speed", mine, TENANT)
        assert m.recent == []
    world(body)


def test_the_assembly_is_held_under_its_ceiling_by_dropping_solutions():
    """Concepts and notes are small, complete and the reason grounding works at all.
    Solutions are numerous and individually replaceable, so they are what gives way."""
    async def body(conn, w):
        a = await context.resolve(conn, "node", w.chapter, TENANT)
        ceiling = context.MAX_MATERIAL_CHARS
        context.MAX_MATERIAL_CHARS = 300
        try:
            m = await context.gather(conn, a, "anything", "nobody", TENANT)
        finally:
            context.MAX_MATERIAL_CHARS = ceiling

        assert len(m.concepts) == 4, "the concepts survived"
        assert len(m.solutions) < 4, "the solutions gave way"
    world(body)


def test_a_scope_with_nothing_in_it_is_recognised_rather_than_asked_about():
    async def body(conn, w):
        empty = f"dc_empty_{uuid.uuid4().hex[:8]}"
        await conn.execute(
            """INSERT INTO nodes (node_id, tenant_id, type, title, slug, depth, status,
                                  parent_id, subject_id)
               VALUES ($1,$2,'topic','Nothing here',$1,2,'published',$3,$4)""",
            empty, TENANT, w.chapter, w.subject)

        a = await context.resolve(conn, "node", empty, TENANT)
        m = await context.gather(conn, a, "explain this", "nobody", TENANT)
        assert m.is_empty()
    world(body)


def test_a_question_mapped_to_several_concepts_is_listed_once():
    """Questions carry more than one concept — the obvious join returns the question once
    per mapping, so two answered questions would come back as six "recent attempts", the
    same two repeated. What the model is told they did has to match what they did."""
    async def body(conn, w):
        uid = f"dc-{uuid.uuid4()}"
        await conn.execute(
            "INSERT INTO users (firebase_uid, tenant_id) VALUES ($1,$2)", uid, TENANT)
        # The same question also belongs to orbital motion.
        await conn.execute(
            """INSERT INTO question_concept_mappings (question_id, concept_node_id,
                                                      is_primary)
               VALUES ($1,$2,FALSE)""",
            w.questions["escape"], w.concepts["orbit"][0])
        await conn.execute(
            """INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id,
                                     is_correct)
               VALUES (gen_random_uuid(), $1, $2, $3, FALSE)""",
            uid, TENANT, w.questions["escape"])

        a = await context.resolve(conn, "node", w.chapter, TENANT)
        m = await context.gather(conn, a, "escape speed", uid, TENANT)

        assert [r.question_id for r in m.recent] == [w.questions["escape"]]
    world(body)


def test_only_the_latest_answer_to_a_question_is_reported():
    """Got it wrong, then right, is a student who now knows it. Telling Jeene they missed
    it would have it re-teach something already fixed."""
    async def body(conn, w):
        uid = f"dc-{uuid.uuid4()}"
        await conn.execute(
            "INSERT INTO users (firebase_uid, tenant_id) VALUES ($1,$2)", uid, TENANT)
        # Explicit times because `now()` is transaction time: two rows written in one
        # transaction share a timestamp, where two real answers never would.
        for minutes, correct in ((10, False), (2, True)):
            await conn.execute(
                """INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id,
                                         is_correct, created_at)
                   VALUES (gen_random_uuid(), $1, $2, $3, $4,
                           now() - make_interval(mins => $5))""",
                uid, TENANT, w.questions["escape"], correct, minutes)

        a = await context.resolve(conn, "node", w.chapter, TENANT)
        m = await context.gather(conn, a, "escape speed", uid, TENANT)

        assert len(m.recent) == 1
        assert m.recent[0].was_correct is True
    world(body)


def test_a_question_is_anchored_to_the_chapter_it_is_primarily_about():
    """Questions are mapped to several concepts and those occasionally sit in different
    chapters. Following whichever mapping came back first would answer the doubt out of
    the wrong chapter, intermittently, which is the worst way for it to be wrong."""
    async def body(conn, w):
        other = World(uuid.uuid4().hex[:8])
        await build(conn, other)
        # The escape-speed question also touches a concept in the other chapter, but it
        # is not what the question is about.
        await conn.execute(
            """INSERT INTO question_concept_mappings (question_id, concept_node_id,
                                                      is_primary)
               VALUES ($1,$2,FALSE)""",
            w.questions["escape"], other.concepts["friction"][0])

        a = await context.resolve(conn, "question", w.questions["escape"], TENANT)
        assert a.chapter_id == w.chapter
    world(body)
