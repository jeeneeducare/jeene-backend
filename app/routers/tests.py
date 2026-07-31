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

    # The answer sheet, so a refresh or a move from phone to browser resumes rather
    # than starting over. Reading the sheet, not the attempt log: nothing is graded
    # until submission.
    saved = await connection.fetch(
        """
        SELECT question_id, selected_option_ids, marked_for_review
        FROM test_responses WHERE session_id = $1
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
        marked_for_review=[r["question_id"] for r in saved if r["marked_for_review"]],
        responses={
            r["question_id"]: list(r["selected_option_ids"] or [])
            for r in saved if r["selected_option_ids"]
        },
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
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TestSessionResult:
    """Finish a sitting: grade the answer sheet and score it.

    This is where the paper becomes attempts — exactly one per answered question,
    graded server-side against the hidden key. One per question is the point: the
    student may have changed an answer five times, but they answered the question
    once, and the attempt log has to say so or every derived number is wrong.

    Idempotent. Submitting twice returns the same result rather than double-recording,
    since a flaky network on the last tap of a three-hour paper must not cost marks.
    """
    session = await connection.fetchrow(
        "SELECT * FROM test_sessions WHERE session_id = $1::uuid AND firebase_uid = $2",
        session_id, user["uid"],
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")

    test = await connection.fetchrow(
        "SELECT test_id, marks_correct, marks_wrong FROM tests WHERE test_id = $1",
        session["test_id"],
    )
    total = await connection.fetchval(
        "SELECT COUNT(*) FROM test_questions WHERE test_id = $1", session["test_id"]
    )

    if session["submitted_at"] is None:
        responses = await connection.fetch(
            """
            SELECT r.question_id, r.selected_option_ids, r.numeric_answer, r.time_spent_ms,
                   q.question_type, q.correct_option_ids,
                   q.numerical_answer, q.numerical_tolerance
            FROM test_responses r
            JOIN questions q ON q.question_id = r.question_id
            WHERE r.session_id = $1::uuid
              AND (r.selected_option_ids IS NOT NULL OR r.numeric_answer IS NOT NULL)
            """,
            session_id,
        )
        for r in responses:
            is_correct = _grade_response(r)
            # A deterministic attempt id per (session, question) makes the whole
            # submission idempotent: a retry collides and does nothing.
            attempt_id = uuid.uuid5(uuid.UUID(str(session["session_id"])), r["question_id"])
            await connection.execute(
                """
                INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id,
                                      session_id, selected_option_ids, numeric_answer,
                                      is_correct, time_spent_ms, solution_revealed)
                VALUES ($1, $2, $3, $4, $5::uuid, $6, $7, $8, $9, false)
                ON CONFLICT (attempt_id) DO NOTHING
                """,
                attempt_id, user["uid"], tenant, r["question_id"], session_id,
                list(r["selected_option_ids"] or []) or None, r["numeric_answer"],
                is_correct, r["time_spent_ms"],
            )

    counts = await connection.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE is_correct) AS correct,
               COUNT(*) FILTER (WHERE NOT is_correct) AS wrong
        FROM attempts WHERE session_id = $1::uuid
        """,
        session_id,
    )
    correct = counts["correct"] or 0
    wrong = counts["wrong"] or 0
    skipped = max((total or 0) - correct - wrong, 0)
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
        total_questions=total or 0,
        correct_count=correct,
        wrong_count=wrong,
        skipped_count=skipped,
        score=score,
        max_score=(total or 0) * test["marks_correct"],
    )


def _grade_response(row) -> bool:
    """Same rule the practice path uses: numericals within tolerance, everything else
    an order-insensitive comparison of the chosen options to the key."""
    if row["question_type"] == "numerical":
        expected, given = row["numerical_answer"], row["numeric_answer"]
        if expected is None or given is None:
            return False
        return abs(float(given) - float(expected)) <= float(row["numerical_tolerance"] or 0)
    key = set(row["correct_option_ids"] or [])
    return bool(key) and set(row["selected_option_ids"] or []) == key


@router.put("/tests/sessions/{session_id}/responses/{question_id}", response_model=TestSession)
async def save_response(
    payload: dict,
    session_id: str,
    question_id: str,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TestSession:
    """Write one answer onto the sheet. Nothing is graded here.

    An answer during a test is a draft: the student may change it as often as they
    like, and only the final state is scored. Grading each save would record several
    attempts for one question, so revising A -> B -> C would read as two wrong answers
    and one right, making a careful student look worse than a lucky one and pushing
    weakness detection at concepts they actually know.

    Sending `selected_option_ids: []` clears the answer, which is the CBT's Clear.
    """
    session = await connection.fetchrow(
        """
        SELECT * FROM test_sessions
        WHERE session_id = $1::uuid AND firebase_uid = $2
        """,
        session_id, user["uid"],
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if session["submitted_at"] is not None:
        raise HTTPException(status_code=409, detail="That test has already been submitted")
    if session["expires_at"] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="That test's time is up")

    belongs = await connection.fetchval(
        "SELECT 1 FROM test_questions WHERE test_id = $1 AND question_id = $2",
        session["test_id"], question_id,
    )
    if not belongs:
        raise HTTPException(status_code=404, detail="That question is not in this paper")

    selected = [str(o) for o in (payload.get("selected_option_ids") or [])]
    await connection.execute(
        """
        INSERT INTO test_responses (session_id, question_id, selected_option_ids,
                                    numeric_answer, marked_for_review, time_spent_ms)
        VALUES ($1::uuid, $2, $3, $4, $5, $6)
        ON CONFLICT (session_id, question_id) DO UPDATE SET
            selected_option_ids = EXCLUDED.selected_option_ids,
            numeric_answer = EXCLUDED.numeric_answer,
            marked_for_review = EXCLUDED.marked_for_review,
            -- Count a genuine change of mind, not a re-save of the same answer.
            revision_count = test_responses.revision_count
                + CASE WHEN test_responses.selected_option_ids IS DISTINCT FROM EXCLUDED.selected_option_ids
                       THEN 1 ELSE 0 END,
            time_spent_ms = test_responses.time_spent_ms + EXCLUDED.time_spent_ms,
            updated_at = now()
        """,
        session_id, question_id, selected or None,
        payload.get("numeric_answer"),
        bool(payload.get("marked_for_review", False)),
        int(payload.get("time_spent_ms") or 0),
    )

    test = await connection.fetchrow(
        "SELECT test_id, title, duration_minutes, marks_correct, marks_wrong FROM tests WHERE test_id = $1",
        session["test_id"],
    )
    return await _session_payload(connection, session, test, tenant)
