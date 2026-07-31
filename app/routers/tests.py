import secrets
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_tenant, require_user
from app.db import get_connection
from app.schemas import (
    TestPaper,
    TestSession,
    TestSessionResult,
    TestSummary,
)

router = APIRouter()

# Unambiguous when read aloud or typed: no O/0, I/1, S/5.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRTUVWXYZ2346789"


def _new_handoff_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


@router.get("/tests", response_model=list[TestSummary])
async def list_tests(
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[TestSummary]:
    """Papers a student can sit. Only published ones, and only those that actually
    have questions — an empty paper is worse than no paper."""
    rows = await connection.fetch(
        """
        SELECT t.test_id, t.title, t.exam, t.class_levels, t.duration_minutes,
               t.marks_correct, t.marks_wrong,
               COUNT(tq.question_id) AS question_count
        FROM tests t
        JOIN test_questions tq ON tq.test_id = t.test_id
        WHERE t.tenant_id = $1 AND t.status = 'published'
        GROUP BY t.test_id
        HAVING COUNT(tq.question_id) > 0
        ORDER BY t.created_at DESC
        """,
        tenant,
    )
    return [
        TestSummary(
            test_id=r["test_id"],
            title=r["title"],
            exam=r["exam"],
            class_levels=list(r["class_levels"] or []),
            duration_minutes=r["duration_minutes"],
            marks_correct=r["marks_correct"],
            marks_wrong=r["marks_wrong"],
            question_count=r["question_count"],
        )
        for r in rows
    ]


async def _paper_for(connection, test_id: str, tenant: str) -> list[dict]:
    """The paper as a student sees it: ordered questions, never the answers.

    The answer key is deliberately absent. During a sitting the only way to learn
    whether a response was right is POST /attempts, which grades server-side, so the
    key cannot be read out of the browser mid-test.
    """
    rows = await connection.fetch(
        """
        SELECT tq.position, tq.section, q.question_id, q.question_type,
               q.question_text, q.options_json, q.difficulty
        FROM test_questions tq
        JOIN questions q ON q.question_id = tq.question_id
        WHERE tq.test_id = $1 AND q.tenant_id = $2
        ORDER BY tq.position
        """,
        test_id,
        tenant,
    )
    return [dict(r) for r in rows]


@router.post("/tests/{test_id}/sessions", response_model=TestSession)
async def start_session(
    test_id: str,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TestSession:
    """Begin a sitting, or resume the one already in progress.

    Starting twice must not silently discard the first attempt's answers, so an
    unsubmitted, unexpired session is handed back rather than replaced.
    """
    test = await connection.fetchrow(
        """
        SELECT test_id, title, duration_minutes, marks_correct, marks_wrong
        FROM tests WHERE test_id = $1 AND tenant_id = $2 AND status = 'published'
        """,
        test_id,
        tenant,
    )
    if test is None:
        raise HTTPException(status_code=404, detail=f"Test '{test_id}' not found")

    existing = await connection.fetchrow(
        """
        SELECT * FROM test_sessions
        WHERE test_id = $1 AND firebase_uid = $2
          AND submitted_at IS NULL AND expires_at > now()
        ORDER BY started_at DESC LIMIT 1
        """,
        test_id,
        user["uid"],
    )
    if existing is not None:
        session = existing
    else:
        session_id = uuid.uuid4()
        expires = datetime.now(timezone.utc) + timedelta(minutes=test["duration_minutes"] or 180)
        # A collision is vanishingly unlikely but would hand one student another's
        # paper, so retry rather than trust the odds.
        for _ in range(5):
            code = _new_handoff_code()
            taken = await connection.fetchval(
                "SELECT 1 FROM test_sessions WHERE handoff_code = $1", code
            )
            if not taken:
                break
        else:
            raise HTTPException(status_code=503, detail="Could not allocate a handoff code")
        session = await connection.fetchrow(
            """
            INSERT INTO test_sessions (session_id, test_id, firebase_uid, tenant_id,
                                       handoff_code, expires_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            RETURNING *
            """,
            session_id, test_id, user["uid"], tenant, code, expires,
        )

    return await _session_payload(connection, session, test, tenant)


async def _session_payload(connection, session, test, tenant) -> TestSession:
    """A session plus everything a client needs to render or resume it."""
    paper = await _paper_for(connection, session["test_id"], tenant)

    # Responses already recorded for this sitting, so a refresh or a move from phone
    # to browser resumes rather than starting over.
    saved = await connection.fetch(
        """
        SELECT DISTINCT ON (question_id) question_id, selected_option_ids
        FROM attempts
        WHERE session_id = $1
        ORDER BY question_id, created_at DESC
        """,
        session["session_id"],
    )
    return TestSession(
        session_id=str(session["session_id"]),
        test_id=session["test_id"],
        title=test["title"],
        handoff_code=session["handoff_code"],
        started_at=session["started_at"],
        expires_at=session["expires_at"],
        submitted_at=session["submitted_at"],
        duration_minutes=test["duration_minutes"],
        marks_correct=test["marks_correct"],
        marks_wrong=test["marks_wrong"],
        marked_for_review=list(session["marked_for_review"] or []),
        responses={r["question_id"]: list(r["selected_option_ids"] or []) for r in saved},
        paper=[
            TestPaper(
                position=p["position"],
                section=p["section"],
                question_id=p["question_id"],
                question_type=p["question_type"],
                question_text=p["question_text"],
                options=p["options_json"],
                difficulty=p["difficulty"],
            )
            for p in paper
        ],
    )


@router.get("/tests/sessions/{session_id}", response_model=TestSession)
async def resume_session(
    session_id: str,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TestSession:
    session = await connection.fetchrow(
        "SELECT * FROM test_sessions WHERE session_id = $1::uuid AND firebase_uid = $2",
        session_id,
        user["uid"],
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    test = await connection.fetchrow(
        "SELECT test_id, title, duration_minutes, marks_correct, marks_wrong FROM tests WHERE test_id = $1",
        session["test_id"],
    )
    return await _session_payload(connection, session, test, tenant)


@router.post("/tests/handoff/{code}", response_model=TestSession)
async def claim_handoff(
    code: str,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TestSession:
    """Exchange a handoff code for its session, so a paper started on the phone can be
    sat in a browser.

    The code is bound to the student who created it: signing in as someone else and
    typing it does nothing. It is a credential for one sitting, not a paper selector.
    """
    session = await connection.fetchrow(
        "SELECT * FROM test_sessions WHERE handoff_code = $1",
        code.strip().upper(),
    )
    if session is None:
        raise HTTPException(status_code=404, detail="That code does not match a test")
    if session["firebase_uid"] != user["uid"]:
        raise HTTPException(status_code=403, detail="That code belongs to a different student")
    if session["submitted_at"] is not None:
        raise HTTPException(status_code=409, detail="That test has already been submitted")
    test = await connection.fetchrow(
        "SELECT test_id, title, duration_minutes, marks_correct, marks_wrong FROM tests WHERE test_id = $1",
        session["test_id"],
    )
    return await _session_payload(connection, session, test, tenant)


@router.post("/tests/sessions/{session_id}/submit", response_model=TestSessionResult)
async def submit_session(
    session_id: str,
    user: dict = Depends(require_user),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TestSessionResult:
    """Finish a sitting and score it.

    The score is computed from the recorded attempts and the paper's own marking
    scheme, never from anything the client sends, so phone and browser always agree
    and a client cannot report its own result.
    """
    session = await connection.fetchrow(
        "SELECT * FROM test_sessions WHERE session_id = $1::uuid AND firebase_uid = $2",
        session_id,
        user["uid"],
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    test = await connection.fetchrow(
        "SELECT marks_correct, marks_wrong FROM tests WHERE test_id = $1",
        session["test_id"],
    )
    totals = await connection.fetchrow(
        """
        WITH latest AS (
            SELECT DISTINCT ON (a.question_id) a.question_id, a.is_correct
            FROM attempts a
            WHERE a.session_id = $1::uuid
            ORDER BY a.question_id, a.created_at DESC
        )
        SELECT
            (SELECT COUNT(*) FROM test_questions WHERE test_id = $2) AS total,
            COUNT(*) FILTER (WHERE is_correct) AS correct,
            COUNT(*) FILTER (WHERE NOT is_correct) AS wrong
        FROM latest
        """,
        session_id,
        session["test_id"],
    )
    correct = totals["correct"] or 0
    wrong = totals["wrong"] or 0
    total = totals["total"] or 0
    skipped = max(total - correct - wrong, 0)
    score = correct * test["marks_correct"] - wrong * test["marks_wrong"]

    updated = await connection.fetchrow(
        """
        UPDATE test_sessions
        SET submitted_at = COALESCE(submitted_at, now()),
            score = $2, correct_count = $3, wrong_count = $4, skipped_count = $5
        WHERE session_id = $1::uuid
        RETURNING submitted_at
        """,
        session_id, score, correct, wrong, skipped,
    )

    return TestSessionResult(
        session_id=session_id,
        test_id=session["test_id"],
        submitted_at=updated["submitted_at"],
        total_questions=total,
        correct_count=correct,
        wrong_count=wrong,
        skipped_count=skipped,
        score=score,
        max_score=total * test["marks_correct"],
    )


@router.put("/tests/sessions/{session_id}/review", response_model=TestSession)
async def set_review_flags(
    payload: dict,
    session_id: str,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TestSession:
    """Persist which questions are marked for review, so the palette survives a move
    between devices. These are presentation flags, not answers."""
    marked = [str(q) for q in (payload.get("marked_for_review") or [])]
    session = await connection.fetchrow(
        """
        UPDATE test_sessions SET marked_for_review = $3
        WHERE session_id = $1::uuid AND firebase_uid = $2
        RETURNING *
        """,
        session_id, user["uid"], marked,
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    test = await connection.fetchrow(
        "SELECT test_id, title, duration_minutes, marks_correct, marks_wrong FROM tests WHERE test_id = $1",
        session["test_id"],
    )
    return await _session_payload(connection, session, test, tenant)
