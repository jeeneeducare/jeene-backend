"""What a plan is allowed to be about.

A student asks for help with a chapter, a topic or a subtopic. That node plus everything
under it is the scope. But a plan that can only ever point inside the scope cannot say
"you need force before gravitation", and that is the thing the feature exists to say. So
the scope is closed over prerequisites too, and the result is the universe of nodes a
plan may reference — checked again in JM-5, because a model that names a node outside
this set has invented content the app cannot open.

Prerequisites come from two places and they are not equally trustworthy:

  * `nodes.prerequisite_node_ids`, written by the pipeline and the teacher. Trusted.
    Today the concept-tree skill fills these only *within* a chapter and leaves
    cross-chapter links to the teacher, so the exact case the feature was asked for is
    the one this source currently under-covers.
  * keyword overlap with earlier chapters in the same subject, computed here. A guess,
    labelled as one, and offered to the planner with a budget rather than as fact.

The second exists because the first is thin, and it is written to be deleted when it
stops being: once cross-chapter prerequisites are authored for a chapter, its derived
candidates mostly stop being selected, and that is the signal to narrow this.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import asyncpg

from app.visibility import NOT_UNRELEASED_TEST_SQL

# A plan is about something a student can point at. A concept is too small to be worth
# a plan of its own, and a subject is not a study session.
SCOPE_TYPES = ("chapter", "topic", "subtopic")

# How far to follow prerequisites of prerequisites. Three is enough to reach the base of
# any real chain in a NEET syllabus, and it bounds the walk whatever the data does —
# `prerequisite_node_ids` is free text as far as Postgres is concerned, so a cycle is a
# data entry mistake away and must not become an infinite recursion.
_PREREQUISITE_DEPTH = 3

# Foundation is meant to be a step or two, not a second syllabus.
_MAX_FOUNDATION_CANDIDATES = 6

# A concept with almost no questions cannot carry a practice step, so offering it as
# groundwork just produces a step that opens nearly empty.
_MIN_FOUNDATION_QUESTIONS = 3


@dataclass
class ResolvedScope:
    """The scope node, everything under it, and the groundwork it may reach for."""

    node: dict
    chapter: dict | None
    subtree: list[dict] = field(default_factory=list)
    concepts: list[dict] = field(default_factory=list)
    subtopics: list[dict] = field(default_factory=list)
    foundation_candidates: list[dict] = field(default_factory=list)

    @property
    def concept_ids(self) -> list[str]:
        return [c["node_id"] for c in self.concepts]

    @property
    def closure_ids(self) -> frozenset[str]:
        """Every node a plan may name. JM-5 validates against exactly this."""
        return frozenset(
            [n["node_id"] for n in self.subtree]
            + [c["node_id"] for c in self.foundation_candidates]
        )


_NODE_COLUMNS = """
    n.node_id, n.type, n.title, n.description, n.parent_id, n.depth, n.display_order,
    n.subject_id, n.class_level, n.ncert_chapter_number, n.pedagogical_notes,
    n.prerequisite_node_ids, n.estimated_minutes, n.difficulty, n.search_keywords
"""

_SCOPE_NODE_SQL = f"""
    SELECT {_NODE_COLUMNS}
      FROM nodes n
     WHERE n.tenant_id = $1 AND n.node_id = $2 AND n.status = 'published'
"""

_SUBTREE_SQL = f"""
    WITH RECURSIVE descendants AS (
        SELECT n.node_id
          FROM nodes n
         WHERE n.tenant_id = $1 AND n.node_id = $2 AND n.status = 'published'
        UNION ALL
        SELECT n.node_id
          FROM nodes n
          JOIN descendants d ON n.parent_id = d.node_id
         WHERE n.tenant_id = $1 AND n.status = 'published'
    )
    SELECT {_NODE_COLUMNS}
      FROM descendants d
      JOIN nodes n ON n.node_id = d.node_id
     ORDER BY n.depth, n.display_order, n.node_id
"""

# Walk up to the enclosing chapter. A subtopic is two hops below one, a topic is one,
# and a chapter is already there — so this is written as a walk rather than as three
# cases, and it keeps working if the tree ever grows a level.
_CHAPTER_SQL = f"""
    WITH RECURSIVE lineage AS (
        SELECT n.node_id, n.parent_id, n.type
          FROM nodes n
         WHERE n.tenant_id = $1 AND n.node_id = $2 AND n.status = 'published'
        UNION ALL
        SELECT n.node_id, n.parent_id, n.type
          FROM nodes n
          JOIN lineage l ON n.node_id = l.parent_id
         WHERE n.tenant_id = $1 AND n.status = 'published'
    )
    SELECT {_NODE_COLUMNS}
      FROM lineage l
      JOIN nodes n ON n.node_id = l.node_id
     WHERE l.type = 'chapter'
     LIMIT 1
"""

# How many published questions a concept can offer a step. Written once because the
# authored and derived candidate queries must agree — a candidate that looks practisable
# to one and empty to the other is a step that opens on nothing.
_CONCEPT_QUESTION_COUNT_SQL = f"""
    (SELECT COUNT(DISTINCT q.question_id)
       FROM question_concept_mappings m
       JOIN questions q ON q.question_id = m.question_id
      WHERE m.concept_node_id = c.node_id
        AND q.tenant_id = $1
        AND q.status = 'published'
        {NOT_UNRELEASED_TEST_SQL}
    )
"""

# The transitive closure of authored prerequisites, bounded by depth.
#
# UNION rather than UNION ALL so a diamond collapses, and the depth guard so a cycle
# terminates: a node listing a descendant as its prerequisite would otherwise recurse
# forever, and nothing in the schema prevents that being written.
#
# Concepts only. The pipeline authors prerequisites concept-to-concept, buckets are
# keyed by concept, and a step needs questions to practise — so a prerequisite written
# against a subtopic is dropped here rather than becoming a candidate nothing can be
# built from. If teachers start writing them at that level, this is what to widen.
_AUTHORED_PREREQUISITES_SQL = f"""
    WITH RECURSIVE closure AS (
        SELECT p.node_id, 1 AS depth
          FROM nodes seed
          CROSS JOIN LATERAL unnest(seed.prerequisite_node_ids) AS p(node_id)
         WHERE seed.tenant_id = $1 AND seed.node_id = ANY($2::text[])
        UNION
        SELECT p.node_id, c.depth + 1
          FROM closure c
          JOIN nodes n ON n.node_id = c.node_id AND n.tenant_id = $1
          CROSS JOIN LATERAL unnest(n.prerequisite_node_ids) AS p(node_id)
         WHERE c.depth < {_PREREQUISITE_DEPTH}
    )
    SELECT c.node_id, c.title, c.description,
           ch.node_id AS chapter_id, ch.title AS chapter_title,
           ch.class_level, ch.ncert_chapter_number,
           {_CONCEPT_QUESTION_COUNT_SQL} AS question_count
      FROM closure cl
      JOIN nodes c  ON c.node_id  = cl.node_id AND c.tenant_id = $1
      JOIN nodes st ON st.node_id = c.parent_id  AND st.tenant_id = $1
      JOIN nodes tp ON tp.node_id = st.parent_id AND tp.tenant_id = $1
      JOIN nodes ch ON ch.node_id = tp.parent_id AND ch.tenant_id = $1
     WHERE c.status = 'published' AND c.type = 'concept' AND ch.status = 'published'
     ORDER BY ch.class_level NULLS LAST, ch.ncert_chapter_number NULLS LAST, c.node_id
"""

# Concepts in earlier chapters of the same subject that talk about the same things.
#
# "Earlier" is (class, chapter number) ordering, and the row comparison yields NULL when
# either side is unknown — which drops the row. That is the right direction to fail: a
# chapter with no number produces no derived groundwork rather than groundwork drawn
# from somewhere later in the book.
_DERIVED_CANDIDATES_SQL = f"""
    WITH scope_keywords AS (
        SELECT ARRAY(
            SELECT DISTINCT lower(k)
              FROM nodes n
              CROSS JOIN LATERAL unnest(n.search_keywords) AS k
             WHERE n.tenant_id = $1 AND n.node_id = ANY($2::text[])
        ) AS kws
    ),
    earlier AS (
        SELECT c.node_id, c.title, c.description,
               ch.node_id AS chapter_id, ch.title AS chapter_title,
               ch.class_level, ch.ncert_chapter_number,
               cardinality(ARRAY(
                   SELECT lower(k) FROM unnest(c.search_keywords) AS k
                   INTERSECT
                   SELECT unnest(kws) FROM scope_keywords
               )) AS shared
          FROM nodes c
          JOIN nodes st ON st.node_id = c.parent_id AND st.tenant_id = $1
          JOIN nodes tp ON tp.node_id = st.parent_id AND tp.tenant_id = $1
          JOIN nodes ch ON ch.node_id = tp.parent_id AND ch.tenant_id = $1
         WHERE c.tenant_id = $1
           AND c.type = 'concept'
           AND c.status = 'published'
           AND ch.status = 'published'
           AND ch.subject_id = $3
           AND (ch.class_level, ch.ncert_chapter_number) < ($4::int, $5::int)
           AND NOT (c.node_id = ANY($6::text[]))
           AND c.search_keywords IS NOT NULL
    ),
    counted AS (
        SELECT c.node_id, c.title, c.description, c.chapter_id, c.chapter_title,
               c.class_level, c.ncert_chapter_number, c.shared,
               {_CONCEPT_QUESTION_COUNT_SQL} AS question_count
          FROM earlier c
         WHERE c.shared > 0
    )
    SELECT node_id, title, description, chapter_id, chapter_title,
           class_level, ncert_chapter_number, shared, question_count
      FROM counted
     WHERE question_count >= $7
     ORDER BY shared DESC, class_level, ncert_chapter_number, node_id
     LIMIT $8
"""


async def resolve_scope(
    connection: asyncpg.Connection, node_id: str, tenant: str
) -> ResolvedScope | None:
    """The scope for `node_id`, or None when there is no published node to plan for.

    None rather than an exception: the caller is an HTTP handler that owns the wording
    of its own 404, and the two callers this will have want different wording.
    """
    row = await connection.fetchrow(_SCOPE_NODE_SQL, tenant, node_id)
    if row is None or row["type"] not in SCOPE_TYPES:
        return None

    node = dict(row)
    subtree = [dict(r) for r in await connection.fetch(_SUBTREE_SQL, tenant, node_id)]
    chapter_row = await connection.fetchrow(_CHAPTER_SQL, tenant, node_id)
    chapter = dict(chapter_row) if chapter_row else None

    concepts = [n for n in subtree if n["type"] == "concept"]
    subtopics = [n for n in subtree if n["type"] == "subtopic"]

    candidates = await _foundation_candidates(
        connection, tenant, node, chapter, subtree, concepts
    )

    return ResolvedScope(
        node=node,
        chapter=chapter,
        subtree=subtree,
        concepts=concepts,
        subtopics=subtopics,
        foundation_candidates=candidates,
    )


async def _foundation_candidates(
    connection: asyncpg.Connection,
    tenant: str,
    node: dict,
    chapter: dict | None,
    subtree: list[dict],
    concepts: list[dict],
) -> list[dict]:
    """Groundwork the plan may reach for, authored first and guesses only to fill up.

    Ordering is the point. An authored prerequisite is a teacher saying this is needed;
    a keyword match is this function noticing two concepts use some of the same words.
    Both are offered, never mixed up, and the authored ones are never crowded out by
    the guesses.
    """
    inside = {n["node_id"] for n in subtree}
    candidates: list[dict] = []
    seen: set[str] = set()

    if concepts:
        authored = await connection.fetch(
            _AUTHORED_PREREQUISITES_SQL, tenant, [c["node_id"] for c in concepts]
        )
        for r in authored:
            # A prerequisite that is already inside the scope is not groundwork — it is
            # part of what the plan teaches, and the planner sees it in `concepts`.
            if r["node_id"] in inside or r["node_id"] in seen:
                continue
            seen.add(r["node_id"])
            candidates.append({**dict(r), "source": "authored"})

    room = _MAX_FOUNDATION_CANDIDATES - len(candidates)
    if room > 0 and concepts and chapter is not None:
        derived = await connection.fetch(
            _DERIVED_CANDIDATES_SQL,
            tenant,
            [c["node_id"] for c in concepts],
            chapter.get("subject_id"),
            chapter.get("class_level"),
            chapter.get("ncert_chapter_number"),
            list(inside | seen),
            _MIN_FOUNDATION_QUESTIONS,
            room,
        )
        for r in derived:
            if r["node_id"] in seen:
                continue
            seen.add(r["node_id"])
            candidates.append({**dict(r), "source": "derived_keywords"})

    return candidates[:_MAX_FOUNDATION_CANDIDATES]
