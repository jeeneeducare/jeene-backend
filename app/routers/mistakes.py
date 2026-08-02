"""The Mistake Book: what a student keeps getting wrong, and where to fix it.

Like the analytics screen, everything here comes out of `attempts`, which holds one row
per answered question whether it was answered in practice or in a mock paper. A test row
carries a `session_id` and a practice row does not, and that is the only difference. The
screen reports the split because students think in those terms, but it never treats the
two as separate weaknesses: getting bulk modulus wrong in a paper and getting it wrong in
practice is one gap, and the ranking counts both.

Two ideas do the work.

**Latest attempt wins.** A question answered wrong in March and right in April is not an
outstanding mistake, and a book that keeps listing it is a book nobody opens twice. So
every count that drives the screen is taken from the most recent attempt per question,
and the difference between "ever wrong" and "still wrong" becomes the progress the
student is actually making.

**The order answers "where should I spend the next hour".** That is two questions, not
one, and ranking on either alone gets it wrong.

Ranked purely by accuracy, one careless slip on the only question you ever tried in a
topic makes it your weakest subject in the world. So confidence comes from the lower
bound of a Wilson interval on the error rate, which is the standard way of asking how
badly wrong this would have to be for the evidence to support it. One miss out of one
scores well below eight out of ten, and the order settles as more questions are answered
rather than lurching about.

But confidence alone puts a topic with two questions outstanding above one with ten,
because two out of two is more certain than ten out of fifteen. Certain is not the same
as worth doing. The score is therefore the confidence weighted by how much is actually
left, square-rooted so that one enormous topic cannot bury everything else and a small
but flatly-failed one still surfaces.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, Query

from app.auth import current_tenant, require_user
from app.db import get_connection
from app.schemas import MistakeBook, SubjectWeakness, WeakTopic

router = APIRouter()

# One-sided 90% confidence. Low enough that a topic seen a handful of times can still
# surface, which matters when a student has only just started, and high enough that a
# single unlucky question does not top the list.
WILSON_Z = 1.2816

# The screen is a place to start work, not an archive. Past twenty rows nobody scrolls,
# and a book that lists everything says nothing about what to do first.
TOPIC_LIMIT = 20


# The most recent attempt per question, which is the only one that describes where the
# student stands now. `DISTINCT ON` with this ordering is Postgres's cheapest way to say
# that. `ever_wrong` is kept alongside so the screen can show what has been turned around.
_LATEST = """
SELECT DISTINCT ON (a.question_id)
       a.question_id,
       a.is_correct,
       a.session_id IS NOT NULL AS in_a_test,
       a.created_at
  FROM attempts a
 WHERE a.firebase_uid = $1 AND a.tenant_id = $2
 ORDER BY a.question_id, a.created_at DESC
"""

# A question is only in the book if a student can still get to it: published, tagged to a
# concept, and in a published chapter. An untagged question is unreachable from the
# browse tree, so listing its topic would send them somewhere that does not exist.
_PLACED = """
SELECT q.question_id,
       topic.node_id    AS topic_id,
       topic.title      AS topic_title,
       chapter.node_id  AS chapter_id,
       chapter.title    AS chapter_title,
       chapter.subject_id,
       concept.node_id  AS concept_id
  FROM questions q
  JOIN question_concept_mappings qcm
       ON qcm.question_id = q.question_id AND qcm.is_primary
  JOIN nodes concept  ON concept.node_id  = qcm.concept_node_id
  JOIN nodes subtopic ON subtopic.node_id = concept.parent_id
  JOIN nodes topic    ON topic.node_id    = subtopic.parent_id
  JOIN nodes chapter  ON chapter.node_id  = topic.parent_id
 WHERE q.tenant_id = $2 AND q.status = 'published' AND chapter.status = 'published'
"""


@router.get("/mistakes", response_model=MistakeBook)
async def get_mistakes(
    subject: str | None = Query(None, description="Restrict to one subject id."),
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> MistakeBook:
    uid = user["uid"]

    # Everything the student has answered, placed in the tree. One question can be
    # tagged to several concepts, so the concepts are gathered per topic further down
    # and the per-question rows are made distinct first.
    rows = await connection.fetch(
        f"""
        WITH latest AS ({_LATEST}),
             placed AS ({_PLACED}),
             answered AS (
                 SELECT DISTINCT
                        l.question_id, l.is_correct, l.in_a_test, l.created_at,
                        p.topic_id, p.topic_title, p.chapter_id, p.chapter_title,
                        p.subject_id
                   FROM latest l JOIN placed p ON p.question_id = l.question_id
             )
        SELECT * FROM answered
         WHERE ($3::text IS NULL OR subject_id = $3)
        """,
        uid, tenant, subject,
    )

    # Whether a question has *ever* been answered wrong, which is what makes the
    # difference between "you got 40 wrong" and "you have 12 left to fix".
    ever = await connection.fetch(
        f"""
        WITH placed AS ({_PLACED})
        SELECT DISTINCT a.question_id
          FROM attempts a JOIN placed p ON p.question_id = a.question_id
         WHERE a.firebase_uid = $1 AND a.tenant_id = $2 AND NOT a.is_correct
           AND ($3::text IS NULL OR p.subject_id = $3)
        """,
        uid, tenant, subject,
    )
    ever_wrong_ids = {r["question_id"] for r in ever}

    subject_titles = {
        r["node_id"]: r["title"]
        for r in await connection.fetch(
            "SELECT node_id, title FROM nodes WHERE tenant_id = $1 AND type = 'subject'",
            tenant,
        )
    }

    # Concepts per topic, so a tapped row can narrow the practice deck without another
    # request. Taken from the tree rather than from the questions the student happened
    # to see, because practising a topic should offer all of it, not only the part they
    # have already met.
    concepts: dict[str, list[str]] = {}
    for r in await connection.fetch(
        """
        SELECT topic.node_id AS topic_id, concept.node_id AS concept_id
          FROM nodes concept
          JOIN nodes subtopic ON subtopic.node_id = concept.parent_id
          JOIN nodes topic    ON topic.node_id    = subtopic.parent_id
         WHERE concept.tenant_id = $1 AND concept.type = 'concept'
        """,
        tenant,
    ):
        concepts.setdefault(r["topic_id"], []).append(r["concept_id"])

    return _assemble(rows, ever_wrong_ids, concepts, subject_titles)


def _wilson_lower_bound(wrong: int, attempted: int) -> float:
    """How high the error rate can be said to be, conservatively.

    A topic with one miss out of one has an observed error rate of 1.0 and almost no
    evidence behind it; this returns about 0.38 for that against about 0.60 for eight
    misses out of ten, which is the ordering the screen wants.
    """
    if attempted <= 0:
        return 0.0
    p = wrong / attempted
    z2 = WILSON_Z * WILSON_Z
    centre = p + z2 / (2 * attempted)
    spread = WILSON_Z * ((p * (1 - p) + z2 / (4 * attempted)) / attempted) ** 0.5
    return max(0.0, (centre - spread) / (1 + z2 / attempted))


def _worth_doing(wrong: int, attempted: int) -> float:
    """How much a topic deserves to be at the top of the list.

    Confidence that the gap is real, multiplied by how much of it is left. The square
    root keeps the second factor from taking over: a topic with sixteen outstanding is
    weighted four times one with a single question, not sixteen times, so a small topic
    the student is failing outright still gets seen.
    """
    return _wilson_lower_bound(wrong, attempted) * (wrong ** 0.5)


def _assemble(rows, ever_wrong_ids, concepts, subject_titles) -> MistakeBook:
    """Fold the per-question rows into the shape the screen draws.

    Done in Python rather than in SQL on purpose. The grouping is over at most a few
    thousand rows per student, and the ranking rule is the one part of this feature
    most likely to be argued about and changed; it should be readable.
    """
    topics: dict[str, dict] = {}
    subjects: dict[str, dict] = {}
    attempted = wrong_now = practice_wrong = test_wrong = 0

    for r in rows:
        attempted += 1
        is_wrong = not r["is_correct"]
        if is_wrong:
            wrong_now += 1
            if r["in_a_test"]:
                test_wrong += 1
            else:
                practice_wrong += 1

        topic = topics.setdefault(r["topic_id"], {
            "topic_id": r["topic_id"], "title": r["topic_title"],
            "chapter_id": r["chapter_id"], "chapter_title": r["chapter_title"],
            "subject_id": r["subject_id"],
            "attempted": 0, "wrong": 0, "unfixed": 0,
            "practice_wrong": 0, "test_wrong": 0, "last_wrong_at": None,
        })
        topic["attempted"] += 1
        if r["question_id"] in ever_wrong_ids:
            topic["wrong"] += 1
        if is_wrong:
            topic["unfixed"] += 1
            topic["practice_wrong"] += 0 if r["in_a_test"] else 1
            topic["test_wrong"] += 1 if r["in_a_test"] else 0
            if topic["last_wrong_at"] is None or r["created_at"] > topic["last_wrong_at"]:
                topic["last_wrong_at"] = r["created_at"]

        slice_ = subjects.setdefault(r["subject_id"], {"attempted": 0, "wrong": 0})
        slice_["attempted"] += 1
        slice_["wrong"] += 1 if is_wrong else 0

    ranked = sorted(
        (t for t in topics.values() if t["unfixed"] > 0),
        key=lambda t: (
            -_worth_doing(t["unfixed"], t["attempted"]),
            -t["unfixed"],
            t["title"],
        ),
    )[:TOPIC_LIMIT]

    return MistakeBook(
        attempted=attempted,
        ever_wrong=len(ever_wrong_ids),
        unfixed=wrong_now,
        fixed=max(0, len(ever_wrong_ids) - wrong_now),
        accuracy=round((attempted - wrong_now) / attempted, 4) if attempted else 0.0,
        practice_wrong=practice_wrong,
        test_wrong=test_wrong,
        topics_affected=sum(1 for t in topics.values() if t["unfixed"] > 0),
        subjects=[
            SubjectWeakness(
                subject_id=sid,
                title=subject_titles.get(sid, sid),
                attempted=s["attempted"],
                wrong=s["wrong"],
                accuracy=round((s["attempted"] - s["wrong"]) / s["attempted"], 4),
            )
            for sid, s in sorted(subjects.items(), key=lambda kv: -kv[1]["wrong"])
            if s["attempted"]
        ],
        topics=[
            WeakTopic(
                topic_id=t["topic_id"],
                title=t["title"],
                chapter_id=t["chapter_id"],
                chapter_title=t["chapter_title"],
                subject_id=t["subject_id"],
                subject_title=subject_titles.get(t["subject_id"], t["subject_id"]),
                concept_ids=concepts.get(t["topic_id"], []),
                attempted=t["attempted"],
                wrong=t["wrong"],
                accuracy=round((t["attempted"] - t["unfixed"]) / t["attempted"], 4),
                unfixed=t["unfixed"],
                practice_wrong=t["practice_wrong"],
                test_wrong=t["test_wrong"],
                last_wrong_at=t["last_wrong_at"],
            )
            for t in ranked
        ],
    )
