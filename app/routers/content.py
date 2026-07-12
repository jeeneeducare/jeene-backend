import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from app.db import get_connection
from app.schemas import (
    Chapter,
    ConceptTag,
    PaginatedQuestions,
    Question,
    QuestionAnswer,
    QuestionFigure,
    TreeNode,
)

TENANT_ID = "JEENE_MASTER"

router = APIRouter()


@router.get("/chapters", response_model=list[Chapter])
async def list_chapters(
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[Chapter]:
    rows = await connection.fetch(
        """
        SELECT node_id, title, ncert_chapter_number, subject_id, class_level
        FROM nodes
        WHERE tenant_id = $1 AND type = 'chapter' AND status = 'published'
        ORDER BY ncert_chapter_number NULLS LAST, title
        """,
        TENANT_ID,
    )
    return [
        Chapter(
            node_id=r["node_id"],
            title=r["title"],
            chapter_number=r["ncert_chapter_number"],
            subject=r["subject_id"],
            class_level=r["class_level"],
        )
        for r in rows
    ]


@router.get("/chapters/{chapter_id}/tree", response_model=TreeNode)
async def get_chapter_tree(
    chapter_id: str,
    connection: asyncpg.Connection = Depends(get_connection),
) -> TreeNode:
    rows = await _fetch_chapter_subtree(connection, chapter_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Chapter '{chapter_id}' not found")
    return _build_tree(rows, chapter_id)


@router.get("/chapters/{chapter_id}/questions", response_model=PaginatedQuestions)
async def list_chapter_questions(
    chapter_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    connection: asyncpg.Connection = Depends(get_connection),
) -> PaginatedQuestions:
    rows = await _fetch_chapter_subtree(connection, chapter_id)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Chapter '{chapter_id}' not found")
    node_ids = [r["node_id"] for r in rows]
    return await _paginated_questions_for_node_ids(connection, node_ids, limit, offset)


@router.get("/concepts/{concept_id}/questions", response_model=PaginatedQuestions)
async def list_concept_questions(
    concept_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    connection: asyncpg.Connection = Depends(get_connection),
) -> PaginatedQuestions:
    exists = await connection.fetchval(
        "SELECT 1 FROM nodes WHERE tenant_id = $1 AND node_id = $2 AND type = 'concept' AND status = 'published'",
        TENANT_ID,
        concept_id,
    )
    if not exists:
        raise HTTPException(status_code=404, detail=f"Concept '{concept_id}' not found")
    return await _paginated_questions_for_node_ids(connection, [concept_id], limit, offset)


@router.get("/questions/{question_id}", response_model=Question)
async def get_question(
    question_id: str,
    connection: asyncpg.Connection = Depends(get_connection),
) -> Question:
    row = await connection.fetchrow(
        """
        SELECT question_id, question_type, question_text, options_json, difficulty
        FROM questions
        WHERE tenant_id = $1 AND question_id = $2 AND status = 'published'
        """,
        TENANT_ID,
        question_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Question '{question_id}' not found")
    figures = await _fetch_figures(connection, [question_id])
    return _row_to_question(row, figures)


@router.get("/questions/{question_id}/answer", response_model=QuestionAnswer)
async def get_question_answer(
    question_id: str,
    connection: asyncpg.Connection = Depends(get_connection),
) -> QuestionAnswer:
    row = await connection.fetchrow(
        """
        SELECT question_id, correct_option_ids, explanation_json
        FROM questions
        WHERE tenant_id = $1 AND question_id = $2 AND status = 'published'
        """,
        TENANT_ID,
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
    connection: asyncpg.Connection, chapter_id: str
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
        TENANT_ID,
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
    connection: asyncpg.Connection, node_ids: list[str], limit: int, offset: int
) -> PaginatedQuestions:
    total = await connection.fetchval(
        """
        SELECT COUNT(DISTINCT q.question_id)
        FROM questions q
        JOIN question_concept_mappings qcm ON qcm.question_id = q.question_id
        WHERE q.tenant_id = $1 AND q.status = 'published' AND qcm.concept_node_id = ANY($2::text[])
        """,
        TENANT_ID,
        node_ids,
    )
    rows = await connection.fetch(
        """
        SELECT DISTINCT q.question_id, q.question_type, q.question_text, q.options_json, q.difficulty
        FROM questions q
        JOIN question_concept_mappings qcm ON qcm.question_id = q.question_id
        WHERE q.tenant_id = $1 AND q.status = 'published' AND qcm.concept_node_id = ANY($2::text[])
        ORDER BY q.question_id
        LIMIT $3 OFFSET $4
        """,
        TENANT_ID,
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
    )
