import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import current_tenant, require_user
from app.db import get_connection
from app.schemas import (
    QuestionExplanation,
    Chapter,
    ConceptTag,
    PaginatedQuestions,
    Question,
    QuestionAnswer,
    ChapterNotes,
    QuestionFigure,
    QuestionHistory,
    TreeNode,
)

router = APIRouter()
# A question that arrived with a test paper stays out of ordinary browsing until a
# test containing it has been released, so a student cannot meet a paper's questions
# in practice before sitting it.
#
# Keyed on the question's own source, NOT merely on membership of an unreleased test.
# A generated paper draws on questions that already live in the bank, and putting one
# of those into an unreleased test must not pull it out of practice — only questions
# that ARRIVED with a paper are gated.
_NOT_UNRELEASED_TEST = """
  AND (q.source <> 'test_paper' OR EXISTS (
        SELECT 1 FROM test_questions tq
        JOIN tests t ON t.test_id = tq.test_id
        WHERE tq.question_id = q.question_id AND t.released_at IS NOT NULL))
"""


@router.get("/chapters", response_model=list[Chapter])
async def list_chapters(
    class_level: int | None = Query(default=None, ge=1),
    exam: str | None = Query(default=None),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[Chapter]:
    rows = await connection.fetch(
        """
        SELECT c.node_id, c.title, c.ncert_chapter_number, c.subject_id, c.class_level,
               s.title AS subject_name
        FROM nodes c
        LEFT JOIN nodes s
            ON s.tenant_id = c.tenant_id AND s.type = 'subject' AND s.subject_id = c.subject_id
        WHERE c.tenant_id = $1 AND c.type = 'chapter' AND c.status = 'published'
          AND ($2::int IS NULL OR c.class_level = $2)
          AND ($3::text IS NULL OR EXISTS (
                SELECT 1 FROM exams e WHERE e.exam_id = $3 AND c.subject_id = ANY(e.subjects)))
        ORDER BY c.subject_id, c.class_level, c.ncert_chapter_number NULLS LAST, c.title
        """,
        tenant,
        class_level,
        exam,
    )
    return [
        Chapter(
            node_id=r["node_id"],
            title=r["title"],
            chapter_number=r["ncert_chapter_number"],
            subject=r["subject_id"],
            subject_name=r["subject_name"],
            class_level=r["class_level"],
        )
        for r in rows
    ]


@router.get("/chapters/{chapter_id}/tree", response_model=TreeNode)
async def get_chapter_tree(
    chapter_id: str,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TreeNode:
    rows = await _fetch_chapter_subtree(connection, chapter_id, tenant)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Chapter '{chapter_id}' not found")
    return _build_tree(rows, chapter_id)


@router.get("/chapters/{chapter_id}/questions", response_model=PaginatedQuestions)
async def list_chapter_questions(
    chapter_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> PaginatedQuestions:
    rows = await _fetch_chapter_subtree(connection, chapter_id, tenant)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Chapter '{chapter_id}' not found")
    node_ids = [r["node_id"] for r in rows]
    return await _paginated_questions_for_node_ids(connection, node_ids, limit, offset, tenant)


@router.get("/chapters/{chapter_id}/notes", response_model=ChapterNotes)
async def chapter_notes(
    chapter_id: str,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> ChapterNotes:
    """The written notes for a chapter, if there are any published.

    404 when a chapter has none, which is most of them today: notes exist for eight
    physics chapters and nowhere else yet. The app asks when a chapter opens so the Notes
    tile can say whether there is anything behind it, rather than letting a student tap
    and find out.
    """
    row = await connection.fetchrow(
        """
        SELECT n.chapter_id, n.title, n.pdf_url, n.page_count, n.size_bytes
          FROM chapter_notes n
          JOIN nodes c ON c.node_id = n.chapter_id
         WHERE n.chapter_id = $1 AND n.tenant_id = $2 AND n.status = 'published'
           AND c.status = 'published'
        """,
        chapter_id, tenant,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No notes for this chapter yet")
    return ChapterNotes(**dict(row))


@router.get("/chapters/{chapter_id}/history", response_model=list[QuestionHistory])
async def chapter_history(
    chapter_id: str,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[QuestionHistory]:
    """What this student has already done with the questions in this chapter.

    The practice deck uses it twice over. Opened normally it redraws each answered
    question exactly as the student left it, right or wrong, rather than presenting work
    they have already done as though it were new. Opened from the Mistake Book it is the
    other way round: the wrong ones are the only ones shown, and they are shown blank.

    Only the most recent attempt per question, because that is the one that describes
    where the student stands. Answering something correctly should replace the record of
    getting it wrong, not sit underneath it.

    Returning the key and the solution here is deliberate and is safe for one specific
    reason: an attempt row only exists once a question has been graded and its answer
    shown. Practice grades on the spot, and a paper writes its attempts at submission,
    never during the sitting. So every question this can speak about is one the student
    has already seen the answer to.
    """
    rows = await _fetch_chapter_subtree(connection, chapter_id, tenant)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Chapter '{chapter_id}' not found")

    history = await connection.fetch(
        """
        SELECT DISTINCT ON (a.question_id)
               a.question_id, a.selected_option_ids, a.is_correct, a.created_at,
               a.session_id IS NOT NULL AS in_a_test,
               q.correct_option_ids, q.explanation_json
          FROM attempts a
          JOIN questions q ON q.question_id = a.question_id
          JOIN question_concept_mappings qcm ON qcm.question_id = q.question_id
         WHERE a.firebase_uid = $1 AND a.tenant_id = $2
           AND qcm.concept_node_id = ANY($3::text[])
           AND q.status = 'published'
        """ + _NOT_UNRELEASED_TEST + """
         ORDER BY a.question_id, a.created_at DESC
        """,
        user["uid"], tenant, [r["node_id"] for r in rows],
    )

    return [
        QuestionHistory(
            question_id=r["question_id"],
            selected_option_ids=list(r["selected_option_ids"] or []),
            is_correct=r["is_correct"],
            correct_option_ids=list(r["correct_option_ids"] or []),
            explanation=(r["explanation_json"] or {}).get("text", ""),
            attempted_at=r["created_at"],
            in_a_test=r["in_a_test"],
        )
        for r in history
    ]


@router.get("/concepts/{concept_id}/questions", response_model=PaginatedQuestions)
async def list_concept_questions(
    concept_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> PaginatedQuestions:
    exists = await connection.fetchval(
        "SELECT 1 FROM nodes WHERE tenant_id = $1 AND node_id = $2 AND type = 'concept' AND status = 'published'",
        tenant,
        concept_id,
    )
    if not exists:
        raise HTTPException(status_code=404, detail=f"Concept '{concept_id}' not found")
    return await _paginated_questions_for_node_ids(connection, [concept_id], limit, offset, tenant)


@router.get("/questions/{question_id}", response_model=Question)
async def get_question(
    question_id: str,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> Question:
    row = await connection.fetchrow(
        """
        SELECT q.question_id, q.question_type, q.question_text, q.options_json, q.difficulty,
               (SELECT array_agg(m.concept_node_id)
                  FROM question_concept_mappings m
                 WHERE m.question_id = q.question_id) AS concept_ids
        FROM questions q
        WHERE q.tenant_id = $1 AND q.question_id = $2 AND q.status = 'published'
        """ + _NOT_UNRELEASED_TEST,
        tenant,
        question_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Question '{question_id}' not found")
    figures = await _fetch_figures(connection, [question_id])
    return _row_to_question(row, figures)


@router.get("/questions/{question_id}/explanation", response_model=QuestionExplanation)
async def get_question_explanation(
    question_id: str,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> QuestionExplanation:
    """A simpler retelling of the worked solution, for a student who is still stuck.

    Served on exactly the same terms as the answer reveal, and gated by the same rule
    for unreleased test papers: an explanation says what the answer is, so a second,
    looser door to the same information would make the first one pointless.

    A plain read. The explanation was written by the pipeline long before anyone asked
    for it, so there is no model call on this path, nothing to rate limit, and nothing
    to keep a student waiting. `draft` explanations are invisible here, exactly like
    every other kind of content.
    """
    row = await connection.fetchrow(
        """
        SELECT e.question_id, e.text
        FROM question_explanations e
        JOIN questions q ON q.question_id = e.question_id
        WHERE q.tenant_id = $1 AND q.question_id = $2
          AND q.status = 'published' AND e.status = 'published'
        """ + _NOT_UNRELEASED_TEST,
        tenant,
        question_id,
    )
    if row is None:
        # Absent rather than empty: most questions will not have one for a while, and
        # the app hides the button rather than offering something that is not there.
        raise HTTPException(
            status_code=404, detail=f"No explanation for '{question_id}' yet"
        )
    return QuestionExplanation(question_id=row["question_id"], text=row["text"])


@router.get("/questions/{question_id}/answer", response_model=QuestionAnswer)
async def get_question_answer(
    question_id: str,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> QuestionAnswer:
    row = await connection.fetchrow(
        """
        SELECT q.question_id, q.correct_option_ids, q.explanation_json
        FROM questions q
        WHERE q.tenant_id = $1 AND q.question_id = $2 AND q.status = 'published'
        """ + _NOT_UNRELEASED_TEST,
        tenant,
        question_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Question '{question_id}' not found")

    concept_rows = await connection.fetch(
        "SELECT concept_node_id, is_primary FROM question_concept_mappings WHERE question_id = $1",
        question_id,
    )
    return QuestionAnswer(
        question_id=row["question_id"],
        correct_option_ids=list(row["correct_option_ids"] or []),
        explanation=row["explanation_json"],
        concepts=[
            ConceptTag(concept_node_id=r["concept_node_id"], is_primary=r["is_primary"])
            for r in concept_rows
        ],
    )


async def _fetch_chapter_subtree(
    connection: asyncpg.Connection, chapter_id: str, tenant: str
) -> list[asyncpg.Record]:
    return await connection.fetch(
        """
        WITH RECURSIVE descendants AS (
            SELECT node_id, type, title, description, parent_id, depth, display_order
            FROM nodes
            WHERE tenant_id = $1 AND node_id = $2 AND type = 'chapter' AND status = 'published'
            UNION ALL
            SELECT n.node_id, n.type, n.title, n.description, n.parent_id, n.depth, n.display_order
            FROM nodes n
            JOIN descendants d ON n.parent_id = d.node_id
            WHERE n.tenant_id = $1 AND n.status = 'published'
        )
        SELECT * FROM descendants ORDER BY depth, display_order
        """,
        tenant,
        chapter_id,
    )


def _build_tree(rows: list[asyncpg.Record], chapter_id: str) -> TreeNode:
    nodes_by_id = {
        r["node_id"]: TreeNode(
            node_id=r["node_id"],
            type=r["type"],
            title=r["title"],
            description=r["description"],
            children=[],
        )
        for r in rows
    }
    for r in rows:
        if r["node_id"] == chapter_id:
            continue
        parent = nodes_by_id.get(r["parent_id"])
        if parent is not None:
            parent.children.append(nodes_by_id[r["node_id"]])
    return nodes_by_id[chapter_id]


async def _paginated_questions_for_node_ids(
    connection: asyncpg.Connection, node_ids: list[str], limit: int, offset: int, tenant: str
) -> PaginatedQuestions:
    total = await connection.fetchval(
        """
        SELECT COUNT(DISTINCT q.question_id)
        FROM questions q
        JOIN question_concept_mappings qcm ON qcm.question_id = q.question_id
        WHERE q.tenant_id = $1 AND q.status = 'published' AND qcm.concept_node_id = ANY($2::text[])
        """ + _NOT_UNRELEASED_TEST,
        tenant,
        node_ids,
    )
    rows = await connection.fetch(
        """
        SELECT DISTINCT q.question_id, q.question_type, q.question_text, q.options_json, q.difficulty,
               (SELECT array_agg(m.concept_node_id)
                  FROM question_concept_mappings m
                 WHERE m.question_id = q.question_id) AS concept_ids
        FROM questions q
        JOIN question_concept_mappings qcm ON qcm.question_id = q.question_id
        WHERE q.tenant_id = $1 AND q.status = 'published' AND qcm.concept_node_id = ANY($2::text[])
        """ + _NOT_UNRELEASED_TEST + """
        ORDER BY q.question_id
        LIMIT $3 OFFSET $4
        """,
        tenant,
        node_ids,
        limit,
        offset,
    )
    question_ids = [r["question_id"] for r in rows]
    figures = await _fetch_figures(connection, question_ids)
    return PaginatedQuestions(
        items=[_row_to_question(r, figures) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


async def _fetch_figures(
    connection: asyncpg.Connection, question_ids: list[str]
) -> dict[str, list[QuestionFigure]]:
    if not question_ids:
        return {}
    rows = await connection.fetch(
        """
        SELECT question_id, image_url, placement, option_id, caption
        FROM question_figures
        WHERE question_id = ANY($1::text[])
        ORDER BY question_id, display_order
        """,
        question_ids,
    )
    figures: dict[str, list[QuestionFigure]] = {}
    for r in rows:
        figures.setdefault(r["question_id"], []).append(
            QuestionFigure(
                image_url=r["image_url"],
                placement=r["placement"],
                option_id=r["option_id"],
                caption=r["caption"],
            )
        )
    return figures


def _row_to_question(
    row: asyncpg.Record, figures_by_question: dict[str, list[QuestionFigure]]
) -> Question:
    return Question(
        question_id=row["question_id"],
        question_type=row["question_type"],
        question_text=row["question_text"],
        options=row["options_json"],
        difficulty=row["difficulty"],
        figures=figures_by_question.get(row["question_id"], []),
        concept_ids=list(row["concept_ids"] or []) if "concept_ids" in row else [],
    )
