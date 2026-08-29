"""The boundary. Everything a planning model is told, and the only place that decides it.

This module is the whole of the content guarantee for Jeene Mode. A model is asked to
sequence material, not to produce it, so it is sent a catalogue: what exists, how much of
it, and how the student has done. It is never sent a question, an option, an answer, a
worked solution, a figure, or a PDF. It cannot leak what it was never given, and no
instruction hidden in a node title can talk it into leaking it either — the leak is not
prevented by the prompt, it is prevented here.

That guarantee is a property of the SELECT lists below and of nothing else, which is why
they name their columns and why `SELECT *` never appears. `app/figures.py` exists for the
same reason and records what happened without it: two routers grew their own copy of the
figure rule, the copies disagreed, and the browse path handed students the answer.

**If you are adding a field, read this.** The test `test_inventory_never_carries_content`
reads the SQL in this module and fails on any of the forbidden columns. It is not in your
way; it is the rule. Adding a content column here is a decision to show a third party the
question bank, and it needs to be made on purpose, by a person, in review.

One structural safeguard beyond the column list: **no question ids cross either.** The
planner is given counts per bucket and answers with a filter, so there is no id for it to
get wrong, no id for it to invent, and no saved plan pointing at a row that has since
been edited. `resolve.py` (JM-3) turns those filters into questions, on this side.
"""

from __future__ import annotations

import asyncpg

from app.plans.record import (
    build_student_record,
    concept_standing,
    scope_question_total,
)
from app.plans.schema import (
    FoundationCandidate,
    Inventory,
    InventoryRollup,
    Materials,
    NotesItem,
    PlacementCheck,
    PlanConstraints,
    QuestionBucket,
    ScopeInfo,
    TeachingNode,
    TestItem,
    VideoItem,
)
from app.plans.scope import ResolvedScope
from app.visibility import NOT_UNRELEASED_TEST_SQL

# Columns that must never appear in a query in this module. Read by the test, so this
# tuple is the executable form of the rule rather than a comment about it.
#
# `question_id` is on the list for a different reason from the rest: it is not content,
# but sending one would let a plan name a specific question, and the selector design
# exists precisely so that it cannot.
FORBIDDEN_COLUMNS = (
    "question_text",
    "options_json",
    "correct_option_ids",
    "explanation_json",
    "numerical_answer",
    "numerical_tolerance",
    "assertion_text",
    "reasoning_text",
    "image_url",
    "pdf_url",
    "alt_text",
    "caption",
)

# Beyond this many buckets a chapter's catalogue stops being something a model can
# reason over and starts being noise, so buckets are rolled up to subtopic level. The
# number is a judgement, not a measurement: revisit it with a real chapter in front of
# you rather than by argument.
_MAX_BUCKETS = 200

# Proficiency is the plan's centre of gravity, not a filter. A basic plan still has to
# reach the target by its checkpoint; this is where it aims, not where it stops.
_TARGET_DIFFICULTY = {
    "basic": "easy",
    "intermediate": "medium",
    "advanced": "hard",
}

# Questions the student can reach, grouped into things that are interchangeable for
# planning. The student's own column is a lateral over the latest attempt, so a uid of
# NULL simply yields nothing and the counts come back zero — the no-history case needs
# no branch.
_BUCKETS_SQL = f"""
    SELECT m.concept_node_id AS node_id,
           q.question_type,
           COALESCE(q.difficulty, 'unrated') AS difficulty,
           COUNT(DISTINCT q.question_id) AS total,
           COUNT(DISTINCT q.question_id)
               FILTER (WHERE att.question_id IS NOT NULL) AS student_attempted,
           COUNT(DISTINCT q.question_id)
               FILTER (WHERE att.is_correct) AS student_correct,
           COUNT(DISTINCT q.question_id)
               FILTER (WHERE ex.question_id IS NOT NULL) AS has_explanations
      FROM questions q
      JOIN question_concept_mappings m ON m.question_id = q.question_id
      LEFT JOIN LATERAL (
          SELECT a.question_id, a.is_correct
            FROM attempts a
           WHERE a.question_id = q.question_id
             AND a.firebase_uid = $3
             AND a.tenant_id = $1
           ORDER BY a.created_at DESC
           LIMIT 1
      ) att ON TRUE
      LEFT JOIN question_explanations ex
             ON ex.question_id = q.question_id AND ex.status = 'published'
     WHERE q.tenant_id = $1
       AND q.status = 'published'
       AND m.concept_node_id = ANY($2::text[])
       {NOT_UNRELEASED_TEST_SQL}
     GROUP BY m.concept_node_id, q.question_type, COALESCE(q.difficulty, 'unrated')
     ORDER BY m.concept_node_id, q.question_type, 3
"""

# Videos hanging anywhere at or below the scope.
_VIDEOS_BELOW_SQL = """
    SELECT v.youtube_id, v.title, v.channel,
           v.node_id AS hangs_on_node_id, n.title AS hangs_on_title,
           n.depth, v.position
      FROM node_videos v
      JOIN nodes n ON n.node_id = v.node_id AND n.tenant_id = $1
     WHERE v.tenant_id = $1
       AND v.status = 'published'
       AND n.status = 'published'
       AND v.node_id = ANY($2::text[])
     ORDER BY n.depth, v.position, v.youtube_id
"""

# Nothing at or below: fall back to the nearest ancestor that has anything. A general
# lecture is better than an empty step, but it comes back marked so the planner can say
# where it came from rather than implying it is about the scope.
_VIDEOS_ABOVE_SQL = """
    WITH RECURSIVE lineage AS (
        SELECT n.node_id, n.parent_id, n.title, 0 AS distance
          FROM nodes n
         WHERE n.tenant_id = $1 AND n.node_id = $2 AND n.status = 'published'
        UNION ALL
        SELECT n.node_id, n.parent_id, n.title, l.distance + 1
          FROM nodes n
          JOIN lineage l ON n.node_id = l.parent_id
         WHERE n.tenant_id = $1 AND n.status = 'published'
    ),
    found AS (
        SELECT v.youtube_id, v.title, v.channel,
               v.node_id AS hangs_on_node_id, l.title AS hangs_on_title,
               l.distance, v.position
          FROM node_videos v
          JOIN lineage l ON l.node_id = v.node_id
         WHERE v.tenant_id = $1 AND v.status = 'published'
    )
    SELECT youtube_id, title, channel, hangs_on_node_id, hangs_on_title, distance
      FROM found
     WHERE distance = (SELECT MIN(distance) FROM found)
     ORDER BY position, youtube_id
"""

# Written notes for the enclosing chapter. Title and length only — the PDF's URL is what
# the reader opens, and it is served by the app, not described to a model.
_NOTES_SQL = """
    SELECT n.chapter_id, n.title, n.page_count
      FROM chapter_notes n
      JOIN nodes c ON c.node_id = n.chapter_id AND c.tenant_id = $1
     WHERE n.chapter_id = $2 AND n.tenant_id = $1
       AND n.status = 'published' AND c.status = 'published'
"""

# Papers that touch the scope. Usually empty: the live papers are ingested full mocks
# with no chapter of their own, so overlap is measured rather than looked up.
_TESTS_SQL = """
    SELECT t.test_id, t.title, t.duration_minutes,
           COUNT(DISTINCT tq.question_id) AS question_count,
           COUNT(DISTINCT tq.question_id) FILTER (
               WHERE EXISTS (
                   SELECT 1 FROM question_concept_mappings m
                    WHERE m.question_id = tq.question_id
                      AND m.concept_node_id = ANY($2::text[])
               )
           ) AS in_scope_question_count
      FROM tests t
      JOIN test_questions tq ON tq.test_id = t.test_id
     WHERE t.tenant_id = $1 AND t.status = 'published'
     GROUP BY t.test_id, t.title, t.duration_minutes
    HAVING COUNT(DISTINCT tq.question_id) FILTER (
               WHERE EXISTS (
                   SELECT 1 FROM question_concept_mappings m
                    WHERE m.question_id = tq.question_id
                      AND m.concept_node_id = ANY($2::text[])
               )
           ) >= $3
     ORDER BY in_scope_question_count DESC, t.test_id
     LIMIT $4
"""

# A paper is only worth offering if a real part of it is about the scope.
_MIN_TEST_OVERLAP = 5
_MAX_TESTS = 3


async def build_inventory(
    connection: asyncpg.Connection,
    scope: ResolvedScope,
    tenant: str,
    *,
    firebase_uid: str | None = None,
    proficiency: str | None = None,
    intent: str | None = None,
    placement: PlacementCheck | None = None,
) -> Inventory:
    """Assemble everything the planner is allowed to know about this scope.

    `firebase_uid` is optional so the admin debug view can read the catalogue without
    pretending to be a student.
    """
    # Sequential, and it has to stay that way: these all share one pooled asyncpg
    # connection, and a connection cannot run two queries at once. Gathering them would
    # not be faster, it would raise. Parallelising this means acquiring more connections,
    # and the pool is capped at five for the whole instance — so it needs a reason.
    concept_ids = scope.concept_ids
    chapter_id = (scope.chapter or {}).get("node_id")

    total_questions = await scope_question_total(connection, tenant, concept_ids)
    buckets, rollup = await _buckets(connection, tenant, concept_ids, firebase_uid, scope)
    # The student's standing on the groundwork, so the planner can tell a gap it has
    # evidence for from one it is only guessing at.
    standing = await concept_standing(
        connection,
        tenant,
        firebase_uid,
        [c["node_id"] for c in scope.foundation_candidates],
    )
    materials = await _materials(connection, tenant, scope, chapter_id, concept_ids)
    student = await build_student_record(
        connection,
        tenant,
        firebase_uid,
        concept_ids,
        total_questions,
        self_reported=proficiency,
        intent=intent,
        placement=placement,
    )

    return Inventory(
        scope=_scope_info(scope),
        concepts=[_teaching_node(n) for n in scope.concepts],
        subtopics=[_teaching_node(n) for n in scope.subtopics],
        foundation_candidates=[
            _candidate(c, standing.get(c["node_id"]))
            for c in scope.foundation_candidates
        ],
        materials=materials,
        question_buckets=buckets,
        scope_question_total=total_questions,
        rollup=rollup,
        student=student,
        constraints=PlanConstraints(
            target_difficulty=_TARGET_DIFFICULTY.get(proficiency or "", "medium"),
            available_question_types=sorted({b.question_type for b in buckets}),
        ),
    )


def _scope_info(scope: ResolvedScope) -> ScopeInfo:
    node = scope.node
    chapter = scope.chapter or {}
    return ScopeInfo(
        node_id=node["node_id"],
        type=node["type"],
        title=node["title"],
        description=node.get("description"),
        chapter_id=chapter.get("node_id"),
        chapter_title=chapter.get("title"),
        subject=node.get("subject_id") or chapter.get("subject_id"),
        class_level=node.get("class_level") or chapter.get("class_level"),
        estimated_minutes=node.get("estimated_minutes"),
        difficulty=node.get("difficulty"),
        pedagogical_notes=node.get("pedagogical_notes"),
    )


def _teaching_node(row: dict) -> TeachingNode:
    return TeachingNode(
        node_id=row["node_id"],
        title=row["title"],
        description=row.get("description"),
        difficulty=row.get("difficulty"),
        estimated_minutes=row.get("estimated_minutes"),
        parent_id=row.get("parent_id"),
        prerequisite_node_ids=list(row.get("prerequisite_node_ids") or []),
    )


def _candidate(
    row: dict, standing: tuple[int, float] | None
) -> FoundationCandidate:
    attempted, accuracy = standing if standing else (0, None)
    return FoundationCandidate(
        node_id=row["node_id"],
        title=row["title"],
        description=row.get("description"),
        chapter_id=row.get("chapter_id"),
        chapter_title=row.get("chapter_title"),
        source=row["source"],
        question_count=row.get("question_count") or 0,
        student_accuracy=accuracy,
        student_attempted=attempted,
    )


async def _buckets(
    connection: asyncpg.Connection,
    tenant: str,
    concept_ids: list[str],
    firebase_uid: str | None,
    scope: ResolvedScope,
) -> tuple[list[QuestionBucket], InventoryRollup]:
    if not concept_ids:
        return [], InventoryRollup()

    rows = await connection.fetch(_BUCKETS_SQL, tenant, concept_ids, firebase_uid)
    buckets = [
        QuestionBucket(
            node_id=r["node_id"],
            question_type=r["question_type"],
            difficulty=r["difficulty"],
            total=r["total"],
            student_attempted=r["student_attempted"],
            student_correct=r["student_correct"],
            has_explanations=r["has_explanations"],
        )
        for r in rows
    ]
    if len(buckets) <= _MAX_BUCKETS:
        return buckets, InventoryRollup(original_bucket_count=len(buckets))

    rolled = _roll_up(buckets, scope)
    # `applied` reports what happened, not what was attempted. A rollup that merged
    # nothing — every bucket already at subtopic level, or concepts whose parent is not
    # in the subtree — would otherwise leave the payload claiming a coarsening that a
    # reader could not find, which is exactly the kind of note that wastes an afternoon.
    if len(rolled) >= len(buckets):
        return buckets, InventoryRollup(original_bucket_count=len(buckets))

    return rolled, InventoryRollup(
        applied=True, level="subtopic", original_bucket_count=len(buckets)
    )


def _roll_up(
    buckets: list[QuestionBucket], scope: ResolvedScope
) -> list[QuestionBucket]:
    """Coarsen concept buckets to their subtopic.

    Totals are summed, which slightly over-counts a question tagged to two concepts under
    the same subtopic. That is the same over-count the un-rolled buckets already carry
    across concepts, and `scope_question_total` remains the honest figure either way —
    but it is worth knowing before anyone reads a rolled-up total as exact.
    """
    parent = {c["node_id"]: c.get("parent_id") for c in scope.concepts}
    merged: dict[tuple[str, str, str], QuestionBucket] = {}
    for b in buckets:
        node_id = parent.get(b.node_id) or b.node_id
        key = (node_id, b.question_type, b.difficulty)
        existing = merged.get(key)
        if existing is None:
            merged[key] = b.model_copy(update={"node_id": node_id})
            continue
        merged[key] = existing.model_copy(
            update={
                "total": existing.total + b.total,
                "student_attempted": existing.student_attempted + b.student_attempted,
                "student_correct": existing.student_correct + b.student_correct,
                "has_explanations": existing.has_explanations + b.has_explanations,
            }
        )
    return sorted(
        merged.values(), key=lambda b: (b.node_id, b.question_type, b.difficulty)
    )


async def _materials(
    connection: asyncpg.Connection,
    tenant: str,
    scope: ResolvedScope,
    chapter_id: str | None,
    concept_ids: list[str],
) -> Materials:
    subtree_ids = [n["node_id"] for n in scope.subtree]

    rows = await connection.fetch(_VIDEOS_BELOW_SQL, tenant, subtree_ids)
    inherited = False
    if not rows:
        rows = await connection.fetch(_VIDEOS_ABOVE_SQL, tenant, scope.node["node_id"])
        # distance 0 is the scope node itself, which the first query already covered.
        rows = [r for r in rows if r["distance"] > 0]
        inherited = True

    videos = [
        VideoItem(
            youtube_id=r["youtube_id"],
            title=r["title"],
            channel=r["channel"] or "",
            hangs_on_node_id=r["hangs_on_node_id"],
            hangs_on_title=r["hangs_on_title"],
            inherited=inherited,
            # JM-2 adds node_videos.duration_seconds; read it here when it exists.
            # Selecting a column before its migration lands breaks every deploy
            # that beats the DDL, so this stays null until then.
            duration_seconds=None,
        )
        for r in rows
    ]

    notes: list[NotesItem] = []
    if chapter_id:
        for r in await connection.fetch(_NOTES_SQL, tenant, chapter_id):
            notes.append(
                NotesItem(
                    chapter_id=r["chapter_id"],
                    title=r["title"],
                    page_count=r["page_count"],
                )
            )

    tests: list[TestItem] = []
    if concept_ids:
        for r in await connection.fetch(
            _TESTS_SQL, tenant, concept_ids, _MIN_TEST_OVERLAP, _MAX_TESTS
        ):
            tests.append(
                TestItem(
                    test_id=r["test_id"],
                    title=r["title"],
                    duration_minutes=r["duration_minutes"],
                    question_count=r["question_count"],
                    in_scope_question_count=r["in_scope_question_count"],
                )
            )

    return Materials(videos=videos, notes=notes, tests=tests)
