"""Turning what a student typed into something they can actually plan.

Everywhere else in Jeene Mode a scope arrives as a node id, because the student got
there by tapping a chapter. Here they typed words instead, and the words have to become
one of the same node ids — or the feature has invented a scope the planner cannot build.

So this is a lookup, not a judgement, and deliberately no model runs in it. Postgres
proposes; the student disposes. `PlanRules.confidentPick` on the app side will skip the
confirmation only for a title the query matched exactly, because picking the wrong
chapter is not a cosmetic mistake: a plan is one of three a student is allowed to have
open, and generating it costs a paid call.

Two normal forms of the same text, because a title can be either kind of thing:

  * `raw` keeps every word, so "motion in a straight line" still equals the title
    *Motion in a Straight Line*, articles and all.
  * `cleaned` drops the words people wrap a request in, so "i want to study
    kinematics" equals the title *Kinematics*.

Neither can carry a `LIKE` wildcard: normalising keeps letters, digits and spaces and
throws the rest away, so `%` and `_` are gone before any SQL sees them. The queries use
`position()` and `left()` rather than `LIKE` anyway, which makes that a second lock
rather than the only one.
"""

from __future__ import annotations

import re

import asyncpg

from app.plans.scope import SCOPE_TYPES
from app.visibility import NOT_UNRELEASED_TEST_SQL

# The words a student wraps a request in. Removing them is what lets a whole sentence
# find a chapter; keeping them is what lets an exact title match still work, which is
# why both forms are kept rather than one replacing the other.
#
# Only words that cannot be part of a NEET syllabus title belong here. "motion",
# "light" and "work" are chapters; "want" and "please" are not.
_STOP_WORDS = frozenset(
    """
    a about all an and any are as at be begin can chapter concept could do doubt for
    from get give go help how i id in into is it its just know learn lesson let like
    make me my need now of on one or please practice prepare question questions
    revise revising revision section start studies study studying subject teach tell
    test that the their them then there these they this those to topic understand up
    us want was we what when where which who why will with would you your
    """.split()
)

# A query longer than this is not a chapter name, it is a paragraph. Bounding it bounds
# the token list and every array the query builds from it.
MAX_QUERY_CHARS = 200

# Enough tokens to name any chapter in the syllabus several times over.
MAX_TOKENS = 12

# A single character matches most of the syllabus and ranks none of it.
MIN_TOKEN_CHARS = 2

# How many candidates to score before the question-count filter runs. Over-fetching is
# what keeps a page of results full when some of the best-scoring nodes turn out to have
# nothing to practise; the multiplier is small because the filter rarely removes much.
_CANDIDATE_MULTIPLIER = 3

DEFAULT_LIMIT = 5
MAX_LIMIT = 10

_KEEP = re.compile(r"[^0-9a-z\s]+")
_SPACES = re.compile(r"\s+")

# The same normalisation as `normalise`, done in SQL to the title being matched against.
# Without it a title carrying punctuation can never match exactly: "p-Block Elements"
# lowercases to "p-block elements", which the query "p block elements" is not equal to
# and is not a substring of. Runs of non-alphanumerics collapse to one space, which is
# what the Python side does to the query, so both sides meet in the same alphabet.
_NORM_TITLE = "btrim(regexp_replace(lower(n.title), '[^0-9a-z]+', ' ', 'g'))"


def normalise(text: str) -> tuple[str, str, list[str]]:
    """`(raw, cleaned, tokens)` — the two phrases to match on, and the words of the second.

    `raw` is the query with punctuation removed and whitespace collapsed. `cleaned` is
    `raw` without the asking-words. `tokens` are `cleaned`'s words, which is what a
    partial match scores against: matching on `raw`'s words would let "i", "to" and
    "the" pull in half the syllabus.
    """
    lowered = _KEEP.sub(" ", (text or "").lower()[:MAX_QUERY_CHARS])
    raw = _SPACES.sub(" ", lowered).strip()
    if not raw:
        return "", "", []
    tokens = [
        word
        for word in raw.split(" ")
        if len(word) >= MIN_TOKEN_CHARS and word not in _STOP_WORDS
    ][:MAX_TOKENS]
    return raw, " ".join(tokens), tokens


# Scores, named because the app depends on the top one meaning "certain". `exact` in the
# response is `score == EXACT`, and that is the only score the app will act on without
# asking the student first.
EXACT = 100
_PREFIX = 80
_PHRASE = 60
_ALL_TOKENS = 40
_SOME_TOKENS = 20
_KEYWORD = 10

# Candidates, scored. `left(...) = $2` rather than `LIKE $2 || '%'` so no value of the
# query can ever be read as a pattern, and `position()` rather than `LIKE '%'||$2||'%'`
# for the same reason.
#
# The recursive part answers the one question that decides whether a result is worth
# offering at all: is there anything under this node to practise? A scope with no
# published questions is one `create_plan` refuses with a 409, so offering it would be
# offering a dead end.
_CANDIDATES_SQL = f"""
WITH RECURSIVE candidates AS (
    SELECT n.node_id, n.type, n.title, n.subject_id, n.class_level,
           CASE
             WHEN {_NORM_TITLE} = $2 OR ($3 <> '' AND {_NORM_TITLE} = $3) THEN {EXACT}
             WHEN $2 <> '' AND left({_NORM_TITLE}, length($2)) = $2 THEN {_PREFIX}
             WHEN $2 <> '' AND position($2 in {_NORM_TITLE}) > 0 THEN {_PHRASE}
             WHEN NOT EXISTS (
                    SELECT 1 FROM unnest($4::text[]) AS t
                     WHERE position(t in {_NORM_TITLE}) = 0
                  ) THEN {_ALL_TOKENS}
             WHEN EXISTS (
                    SELECT 1 FROM unnest($4::text[]) AS t
                     WHERE position(t in {_NORM_TITLE}) > 0
                  ) THEN {_SOME_TOKENS}
             ELSE {_KEYWORD}
           END AS score,
           CASE n.type WHEN 'chapter' THEN 0 WHEN 'topic' THEN 1 ELSE 2 END AS type_rank
      FROM nodes n
     WHERE n.tenant_id = $1
       AND n.type = ANY($5::text[])
       AND n.status = 'published'
       AND ($6::int IS NULL OR n.class_level = $6)
       AND ($7::text IS NULL OR EXISTS (
              SELECT 1 FROM exams e
               -- Case-insensitive, for the reason content.py's chapter list is.
               WHERE lower(e.exam_id) = lower($7) AND n.subject_id = ANY(e.subjects)))
       AND (
              EXISTS (SELECT 1 FROM unnest($4::text[]) AS t
                       WHERE position(t in {_NORM_TITLE}) > 0)
           OR ($2 <> '' AND position($2 in {_NORM_TITLE}) > 0)
           OR EXISTS (SELECT 1
                        FROM unnest(coalesce(n.search_keywords, ARRAY[]::text[])) AS k,
                             unnest($4::text[]) AS t
                       WHERE btrim(regexp_replace(lower(k), '[^0-9a-z]+', ' ', 'g')) = t)
       )
     ORDER BY score DESC, type_rank, length(n.title), n.title
     LIMIT $8
),
descendants AS (
    SELECT c.node_id AS root_id, c.node_id, c.type
      FROM candidates c
    UNION ALL
    SELECT d.root_id, n.node_id, n.type
      FROM nodes n
      JOIN descendants d ON n.parent_id = d.node_id
     WHERE n.tenant_id = $1 AND n.status = 'published'
),
counts AS (
    SELECT d.root_id, COUNT(DISTINCT q.question_id) AS question_count
      FROM descendants d
      JOIN question_concept_mappings m ON m.concept_node_id = d.node_id
      JOIN questions q ON q.question_id = m.question_id
     -- Concepts only, because that is what the planner counts: `resolve_scope` takes
     -- `[n for n in subtree if n.type == 'concept']` and the inventory's buckets are
     -- built from exactly those. `question_concept_mappings.concept_node_id` is only
     -- `REFERENCES nodes(node_id)` — nothing in the schema stops a mapping pointing at
     -- a subtopic — so counting every descendant type would let this promise a scope
     -- whose buckets come back empty, which is the 409 this filter exists to prevent.
     WHERE d.type = 'concept'
       AND q.tenant_id = $1
       AND q.status = 'published'
       {NOT_UNRELEASED_TEST_SQL}
     GROUP BY d.root_id
)
SELECT c.node_id, c.type, c.title, c.subject_id, c.class_level, c.score,
       k.question_count
  FROM candidates c
  JOIN counts k ON k.root_id = c.node_id
 ORDER BY c.score DESC, c.type_rank, length(c.title), c.title
 LIMIT $9
"""

# Where each result sits: the chapter above it, and the subject's readable name. The
# chapter id is not decoration — the app passes it to `beginIntake`, which reads the
# student's record for that chapter to decide whether to offer the placement check.
_CONTEXT_SQL = """
WITH RECURSIVE ancestry AS (
    SELECT n.node_id AS root_id, n.node_id, n.parent_id, n.type, n.title
      FROM nodes n
     WHERE n.tenant_id = $1 AND n.node_id = ANY($2::text[])
    UNION ALL
    SELECT a.root_id, n.node_id, n.parent_id, n.type, n.title
      FROM nodes n
      JOIN ancestry a ON n.node_id = a.parent_id
     WHERE n.tenant_id = $1 AND n.status = 'published'
)
SELECT DISTINCT ON (a.root_id)
       a.root_id, a.node_id AS chapter_node_id, a.title AS chapter_title
  FROM ancestry a
 WHERE a.type = 'chapter'
 -- A node has exactly one chapter above it in a tree, so this orders a set of one.
 -- Written anyway: DISTINCT ON without ORDER BY picks an unspecified row, and a query
 -- whose correctness rests on the data never growing a second parent is a trap.
 ORDER BY a.root_id, a.node_id
"""

_SUBJECT_SQL = """
SELECT subject_id, title
  FROM nodes
 WHERE tenant_id = $1 AND type = 'subject' AND subject_id = ANY($2::text[])
"""


async def search_scopes(
    connection: asyncpg.Connection,
    tenant: str,
    query: str,
    limit: int = DEFAULT_LIMIT,
    class_level: int | None = None,
    exam: str | None = None,
) -> list[dict]:
    """Plannable scopes matching `query`, best first.

    Empty is an ordinary answer: a student can type something that is not in the
    syllabus, and that is not an error to raise, it is a result to show.
    """
    raw, cleaned, tokens = normalise(query)
    if not tokens:
        return []

    limit = max(1, min(limit, MAX_LIMIT))
    rows = await connection.fetch(
        _CANDIDATES_SQL,
        tenant,
        raw,
        cleaned,
        tokens,
        list(SCOPE_TYPES),
        class_level,
        exam,
        limit * _CANDIDATE_MULTIPLIER,
        limit,
    )
    if not rows:
        return []

    node_ids = [r["node_id"] for r in rows]
    chapters = {
        r["root_id"]: (r["chapter_node_id"], r["chapter_title"])
        for r in await connection.fetch(_CONTEXT_SQL, tenant, node_ids)
    }
    subject_ids = sorted({r["subject_id"] for r in rows if r["subject_id"]})
    subjects = {
        r["subject_id"]: r["title"]
        for r in (
            await connection.fetch(_SUBJECT_SQL, tenant, subject_ids)
            if subject_ids
            else []
        )
    }

    results = []
    for row in rows:
        chapter_id, chapter_title = chapters.get(row["node_id"], (None, None))
        results.append(
            {
                "node_id": row["node_id"],
                "title": row["title"],
                "type": row["type"],
                "chapter_node_id": chapter_id,
                "chapter_title": chapter_title,
                "subject_id": row["subject_id"],
                "subject_name": subjects.get(row["subject_id"]),
                "class_level": row["class_level"],
                "question_count": row["question_count"],
                "exact": row["score"] == EXACT,
            }
        )
    return results
