import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_tenant, require_user
from app.db import get_connection
from app.schemas import (
    AttemptRequest,
    AttemptResult,
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


@router.get("/progress", response_model=ProgressSummary)
async def get_progress(
    user: dict = Depends(require_user),
    connection: asyncpg.Connection = Depends(get_connection),
) -> ProgressSummary:
    """Everything the home screen and profile need, derived from the attempt log:
    overall totals plus a breakdown by subject and by chapter."""
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

    # An attempt reaches its chapter through the concept it is tagged to, so walk
    # concept -> subtopic -> topic -> chapter and roll up there.
    rows = await connection.fetch(
        """
        WITH scoped AS (
            SELECT DISTINCT ON (a.attempt_id)
                   a.attempt_id, a.is_correct, a.time_spent_ms,
                   n.subject_id, chapter.node_id AS chapter_id, chapter.title AS chapter_title
            FROM attempts a
            JOIN question_concept_mappings qcm ON qcm.question_id = a.question_id
            JOIN nodes n       ON n.node_id = qcm.concept_node_id
            JOIN nodes subtop  ON subtop.node_id = n.parent_id
            JOIN nodes topic   ON topic.node_id = subtop.parent_id
            JOIN nodes chapter ON chapter.node_id = topic.parent_id
            WHERE a.firebase_uid = $1
            ORDER BY a.attempt_id, qcm.is_primary DESC
        )
        SELECT subject_id, chapter_id, chapter_title,
               COUNT(*) AS attempted,
               COUNT(*) FILTER (WHERE is_correct) AS correct,
               COALESCE(SUM(time_spent_ms), 0) AS time_spent_ms
        FROM scoped
        GROUP BY subject_id, chapter_id, chapter_title
        """,
        user["uid"],
    )

    chapters = [
        ScopeProgress(
            node_id=r["chapter_id"],
            title=r["chapter_title"],
            attempted=r["attempted"],
            correct=r["correct"],
            accuracy=_accuracy(r["correct"], r["attempted"]),
            time_spent_ms=r["time_spent_ms"],
        )
        for r in rows
    ]

    by_subject: dict[str, dict] = {}
    for r in rows:
        bucket = by_subject.setdefault(
            r["subject_id"], {"attempted": 0, "correct": 0, "time_spent_ms": 0}
        )
        bucket["attempted"] += r["attempted"]
        bucket["correct"] += r["correct"]
        bucket["time_spent_ms"] += r["time_spent_ms"]

    subjects = [
        ScopeProgress(
            node_id=subject_id,
            title=subject_id,
            attempted=v["attempted"],
            correct=v["correct"],
            accuracy=_accuracy(v["correct"], v["attempted"]),
            time_spent_ms=v["time_spent_ms"],
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


@router.get("/progress/chapters/{chapter_id}", response_model=list[ConceptProgress])
async def get_chapter_progress(
    chapter_id: str,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[ConceptProgress]:
    """Per-concept accuracy inside one chapter. This is the shape weakness
    detection and adaptive practice will read from."""
    rows = await connection.fetch(
        """
        WITH RECURSIVE descendants AS (
            SELECT node_id, type, title, parent_id
            FROM nodes
            WHERE tenant_id = $1 AND node_id = $2
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
        LEFT JOIN attempts a
               ON a.question_id = qcm.question_id AND a.firebase_uid = $3
        WHERE d.type = 'concept'
        GROUP BY d.node_id, d.title
        ORDER BY d.node_id
        """,
        tenant,
        chapter_id,
        user["uid"],
    )
    return [
        ConceptProgress(
            node_id=r["node_id"],
            title=r["title"],
            attempted=r["attempted"],
            correct=r["correct"],
            accuracy=_accuracy(r["correct"], r["attempted"]),
        )
        for r in rows
    ]
