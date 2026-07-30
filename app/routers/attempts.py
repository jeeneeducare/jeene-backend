import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_tenant, require_user
from app.db import get_connection
from app.schemas import (
    AttemptRequest,
    AttemptResult,
    ChapterProgressDetail,
    ConceptProgress,
    ProgressSummary,
    ScopeProgress,
)

router = APIRouter()


def _accuracy(correct: int, attempted: int) -> float:
    return round(correct / attempted, 4) if attempted else 0.0


@router.post("/attempts", response_model=AttemptResult)
async def submit_attempt(
    body: AttemptRequest,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> AttemptResult:
    """Record one answer, graded server-side against the hidden key.

    The client never sends whether it was right; it sends what the student chose.
    Practice asks for the solution back in the same round trip (so answering costs
    exactly one request); a test omits it and reveals only at the end.
    """
    question = await connection.fetchrow(
        """
        SELECT question_id, question_type, correct_option_ids, explanation_json,
               numerical_answer, numerical_tolerance
        FROM questions
        WHERE tenant_id = $1 AND question_id = $2 AND status = 'published'
        """,
        tenant,
        body.question_id,
    )
    if question is None:
        raise HTTPException(status_code=404, detail=f"Question '{body.question_id}' not found")

    is_correct = _grade(question, body)

    row = await connection.fetchrow(
        """
        INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id, session_id,
                              selected_option_ids, numeric_answer, is_correct,
                              time_spent_ms, solution_revealed)
        VALUES ($1::uuid, $2, $3, $4, $5::uuid, $6, $7, $8, $9, $10)
        ON CONFLICT (attempt_id) DO NOTHING
        RETURNING attempt_id
        """,
        body.attempt_id,
        user["uid"],
        tenant,
        body.question_id,
        body.session_id,
        body.selected_option_ids or None,
        body.numeric_answer,
        is_correct,
        body.time_spent_ms,
        body.include_solution,
    )
    # No row back means this attempt_id was already stored: a retry, not a new answer.
    already_recorded = row is None

    return AttemptResult(
        attempt_id=body.attempt_id,
        question_id=body.question_id,
        is_correct=is_correct,
        correct_option_ids=(
            list(question["correct_option_ids"] or []) if body.include_solution else None
        ),
        explanation=question["explanation_json"] if body.include_solution else None,
        already_recorded=already_recorded,
    )


def _grade(question: asyncpg.Record, body: AttemptRequest) -> bool:
    """Numericals compare within the authored tolerance; everything else compares
    the chosen option set to the key, order-insensitively."""
    if question["question_type"] == "numerical":
        expected = question["numerical_answer"]
        if expected is None or body.numeric_answer is None:
            return False
        tolerance = question["numerical_tolerance"] or 0
        return abs(float(body.numeric_answer) - float(expected)) <= float(tolerance)

    key = set(question["correct_option_ids"] or [])
    if not key:
        return False
    return set(body.selected_option_ids) == key


# One row per (question, chapter) that exists in the published tree, joined to what
# this student has done with it. Coverage needs the denominator — how many questions
# exist in a scope — which only this shape gives.
_SCOPE_PROGRESS_SQL = """
WITH scope AS (
    SELECT DISTINCT q.question_id,
           chapter.node_id  AS chapter_id,
           chapter.title    AS chapter_title,
           chapter.subject_id
    FROM questions q
    JOIN question_concept_mappings qcm ON qcm.question_id = q.question_id
    JOIN nodes concept  ON concept.node_id = qcm.concept_node_id
    JOIN nodes subtopic ON subtopic.node_id = concept.parent_id
    JOIN nodes topic    ON topic.node_id = subtopic.parent_id
    JOIN nodes chapter  ON chapter.node_id = topic.parent_id
    WHERE q.tenant_id = $1 AND q.status = 'published'
),
mine AS (
    SELECT question_id,
           COUNT(*)                       AS tries,
           COUNT(*) FILTER (WHERE is_correct) AS hits,
           BOOL_OR(is_correct)            AS ever_correct,
           COALESCE(SUM(time_spent_ms), 0) AS ms
    FROM attempts
    WHERE firebase_uid = $2
    GROUP BY question_id
)
SELECT s.subject_id, s.chapter_id, s.chapter_title,
       COUNT(*)                                            AS total_questions,
       COUNT(m.question_id)                                AS attempted_questions,
       COUNT(m.question_id) FILTER (WHERE m.ever_correct)  AS solved_questions,
       COALESCE(SUM(m.tries), 0)                           AS attempted,
       COALESCE(SUM(m.hits), 0)                            AS correct,
       COALESCE(SUM(m.ms), 0)                              AS time_spent_ms
FROM scope s
LEFT JOIN mine m ON m.question_id = s.question_id
GROUP BY s.subject_id, s.chapter_id, s.chapter_title
"""


@router.get("/progress", response_model=ProgressSummary)
async def get_progress(
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> ProgressSummary:
    """Everything the home screen and profile need, derived from the attempt log:
    overall totals plus per-subject and per-chapter accuracy, time and coverage."""
    totals = await connection.fetchrow(
        """
        SELECT COUNT(*) AS attempted,
               COUNT(*) FILTER (WHERE is_correct) AS correct,
               COALESCE(SUM(time_spent_ms), 0) AS time_spent_ms,
               COUNT(DISTINCT question_id) AS distinct_questions
        FROM attempts WHERE firebase_uid = $1
        """,
        user["uid"],
    )

    rows = await connection.fetch(_SCOPE_PROGRESS_SQL, tenant, user["uid"])

    chapters = [
        ScopeProgress(
            node_id=r["chapter_id"],
            title=r["chapter_title"],
            attempted=r["attempted"],
            correct=r["correct"],
            accuracy=_accuracy(r["correct"], r["attempted"]),
            time_spent_ms=r["time_spent_ms"],
            total_questions=r["total_questions"],
            attempted_questions=r["attempted_questions"],
            solved_questions=r["solved_questions"],
        )
        for r in rows
    ]

    by_subject: dict[str, dict] = {}
    for r in rows:
        b = by_subject.setdefault(
            r["subject_id"],
            {"attempted": 0, "correct": 0, "time_spent_ms": 0,
             "total_questions": 0, "attempted_questions": 0, "solved_questions": 0},
        )
        for field in b:
            b[field] += r[field]

    subjects = [
        ScopeProgress(
            node_id=subject_id,
            title=subject_id,
            attempted=v["attempted"],
            correct=v["correct"],
            accuracy=_accuracy(v["correct"], v["attempted"]),
            time_spent_ms=v["time_spent_ms"],
            total_questions=v["total_questions"],
            attempted_questions=v["attempted_questions"],
            solved_questions=v["solved_questions"],
        )
        for subject_id, v in sorted(by_subject.items())
    ]

    return ProgressSummary(
        attempted=totals["attempted"],
        correct=totals["correct"],
        accuracy=_accuracy(totals["correct"], totals["attempted"]),
        time_spent_ms=totals["time_spent_ms"],
        distinct_questions=totals["distinct_questions"],
        subjects=subjects,
        chapters=chapters,
    )


# A scope's questions reached through the tree. Topics sit two hops above concepts,
# subtopics one — otherwise the roll-up is identical, so it is written once.
_TOPIC_SCOPE = """
    FROM nodes scope
    JOIN nodes subtopic ON subtopic.parent_id = scope.node_id
    JOIN nodes concept  ON concept.parent_id = subtopic.node_id
    JOIN question_concept_mappings qcm ON qcm.concept_node_id = concept.node_id
    JOIN questions q ON q.question_id = qcm.question_id
                    AND q.tenant_id = $1 AND q.status = 'published'
    WHERE scope.parent_id = $2 AND scope.tenant_id = $1
"""

_SUBTOPIC_SCOPE = """
    FROM nodes scope
    JOIN nodes concept ON concept.parent_id = scope.node_id
    JOIN question_concept_mappings qcm ON qcm.concept_node_id = concept.node_id
    JOIN questions q ON q.question_id = qcm.question_id
                    AND q.tenant_id = $1 AND q.status = 'published'
    JOIN nodes topic ON topic.node_id = scope.parent_id
    WHERE topic.parent_id = $2 AND scope.tenant_id = $1
"""


def _scope_level_sql(scope_join: str) -> str:
    """Coverage and accuracy for every node at one level of a chapter.

    DISTINCT on (scope, question) matters: a question tagged to several concepts under
    the same scope must count once, which is a dedup the client cannot do.
    """
    return f"""
        WITH scope_questions AS (
            SELECT DISTINCT scope.node_id AS scope_id, scope.title AS scope_title,
                   scope.display_order, q.question_id
            {scope_join}
        ),
        mine AS (
            SELECT question_id,
                   COUNT(*) AS tries,
                   COUNT(*) FILTER (WHERE is_correct) AS hits,
                   BOOL_OR(is_correct) AS ever_correct,
                   COALESCE(SUM(time_spent_ms), 0) AS ms
            FROM attempts WHERE firebase_uid = $3 GROUP BY question_id
        )
        SELECT sq.scope_id, sq.scope_title, sq.display_order,
               COUNT(*) AS total_questions,
               COUNT(m.question_id) AS attempted_questions,
               COUNT(m.question_id) FILTER (WHERE m.ever_correct) AS solved_questions,
               COALESCE(SUM(m.tries), 0) AS attempted,
               COALESCE(SUM(m.hits), 0) AS correct,
               COALESCE(SUM(m.ms), 0) AS time_spent_ms
        FROM scope_questions sq
        LEFT JOIN mine m ON m.question_id = sq.question_id
        GROUP BY sq.scope_id, sq.scope_title, sq.display_order
        ORDER BY sq.display_order, sq.scope_title
    """


def _scope_progress(row: asyncpg.Record) -> ScopeProgress:
    return ScopeProgress(
        node_id=row["scope_id"],
        title=row["scope_title"],
        attempted=row["attempted"],
        correct=row["correct"],
        accuracy=_accuracy(row["correct"], row["attempted"]),
        time_spent_ms=row["time_spent_ms"],
        total_questions=row["total_questions"],
        attempted_questions=row["attempted_questions"],
        solved_questions=row["solved_questions"],
    )


@router.get("/progress/chapters/{chapter_id}", response_model=ChapterProgressDetail)
async def get_chapter_progress(
    chapter_id: str,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> ChapterProgressDetail:
    """One chapter broken down by topic (what the topic cards show) and by concept
    (the shape weakness detection and adaptive practice will read).

    Topic coverage is computed here rather than in the app because a question can
    be tagged to several concepts under the same topic; counting it once needs a
    DISTINCT the client cannot do without the mapping table.
    """
    topic_rows = await connection.fetch(
        _scope_level_sql(_TOPIC_SCOPE), tenant, chapter_id, user["uid"]
    )
    # One level deeper. The subtopic tree screen shows coverage per subtopic, so the
    # same roll-up runs with concepts one hop closer to the scope.
    subtopic_rows = await connection.fetch(
        _scope_level_sql(_SUBTOPIC_SCOPE), tenant, chapter_id, user["uid"]
    )

    concept_rows = await connection.fetch(
        """
        WITH RECURSIVE descendants AS (
            SELECT node_id, type, title, parent_id
            FROM nodes WHERE tenant_id = $1 AND node_id = $2
            UNION ALL
            SELECT n.node_id, n.type, n.title, n.parent_id
            FROM nodes n JOIN descendants d ON n.parent_id = d.node_id
            WHERE n.tenant_id = $1
        )
        SELECT d.node_id, d.title,
               COUNT(a.attempt_id) AS attempted,
               COUNT(a.attempt_id) FILTER (WHERE a.is_correct) AS correct
        FROM descendants d
        LEFT JOIN question_concept_mappings qcm ON qcm.concept_node_id = d.node_id
        LEFT JOIN attempts a ON a.question_id = qcm.question_id AND a.firebase_uid = $3
        WHERE d.type = 'concept'
        GROUP BY d.node_id, d.title
        ORDER BY d.node_id
        """,
        tenant,
        chapter_id,
        user["uid"],
    )

    topics = [_scope_progress(r) for r in topic_rows]
    subtopics = [_scope_progress(r) for r in subtopic_rows]

    # Deliberately NOT the sum of the topics: a question tagged under two topics
    # counts once per topic (correct for topic coverage) but must count once for
    # the chapter, or this endpoint would contradict /progress.
    chapter_row = await connection.fetchrow(
        _SCOPE_PROGRESS_SQL + " HAVING s.chapter_id = $3",
        tenant,
        user["uid"],
        chapter_id,
    )
    chapter = ScopeProgress(
        node_id=chapter_id,
        title=chapter_row["chapter_title"] if chapter_row else "",
        attempted=chapter_row["attempted"] if chapter_row else 0,
        correct=chapter_row["correct"] if chapter_row else 0,
        accuracy=_accuracy(
            chapter_row["correct"] if chapter_row else 0,
            chapter_row["attempted"] if chapter_row else 0,
        ),
        time_spent_ms=chapter_row["time_spent_ms"] if chapter_row else 0,
        total_questions=chapter_row["total_questions"] if chapter_row else 0,
        attempted_questions=chapter_row["attempted_questions"] if chapter_row else 0,
        solved_questions=chapter_row["solved_questions"] if chapter_row else 0,
    )

    return ChapterProgressDetail(
        chapter=chapter,
        topics=topics,
        subtopics=subtopics,
        concepts=[
            ConceptProgress(
                node_id=r["node_id"],
                title=r["title"],
                attempted=r["attempted"],
                correct=r["correct"],
                accuracy=_accuracy(r["correct"], r["attempted"]),
            )
            for r in concept_rows
        ],
    )
