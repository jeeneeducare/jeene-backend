import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.auth import current_tenant, optional_user, require_user
from app.db import get_connection
from app.schemas import (
    QuestionFigure,
    QuestionResult,
    TestPaper,
    TestSession,
    TestSessionResult,
    TestSummary,
)

router = APIRouter()

# Claim attempts per client, so guessing a code is visibly futile rather than merely
# impractical. In-memory is enough: the window is a minute and a restart only forgives.
_claim_attempts: dict[str, list[float]] = {}
_CLAIM_LIMIT = 10
_CLAIM_WINDOW_SECONDS = 60


def _rate_limit_claim(client: str) -> None:
    now = time.monotonic()
    recent = [t for t in _claim_attempts.get(client, []) if now - t < _CLAIM_WINDOW_SECONDS]
    if len(recent) >= _CLAIM_LIMIT:
        raise HTTPException(status_code=429, detail="Too many attempts. Wait a minute.")
    recent.append(now)
    _claim_attempts[client] = recent


async def _authorized_session(
    connection: asyncpg.Connection,
    session_id: str,
    user: dict | None,
    web_token: str | None,
):
    """A sitting may be driven by the student's own token, or by the browser token
    handed out when its code was claimed.

    The browser is deliberately not asked to sign in: the code it was given was minted
    by a signed-in student, so requiring identity again re-proves what the code already
    proved, and does it while the clock runs. The token it gets back grants exactly this
    sitting — no account, no other paper, no history.
    """
    session = await connection.fetchrow(
        "SELECT * FROM test_sessions WHERE session_id = $1::uuid", session_id
    )
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if web_token and session["web_token"] and secrets.compare_digest(web_token, session["web_token"]):
        return session
    if user is not None and session["firebase_uid"] == user["uid"]:
        return session
    raise HTTPException(status_code=404, detail="Session not found")

# Unambiguous when read aloud or typed: no O/0, I/1, S/5.
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRTUVWXYZ2346789"


def _new_handoff_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(6))


@router.get("/tests", response_model=list[TestSummary])
async def list_tests(
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[TestSummary]:
    """Papers a student can sit. Only published ones, and only those that actually
    have questions — an empty paper is worse than no paper.

    Signed in only. Sitting a paper is account-bound, so listing should be too, and
    a coaching's test series is theirs: with the tenant taken from the caller's token,
    an anonymous request would otherwise read the default tenant's papers.
    """
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
    paper = [dict(r) for r in rows]
    figures = await _figures_for(connection, [r["question_id"] for r in paper])
    for question in paper:
        question["figures"] = figures.get(question["question_id"], [])
    return paper


async def _figures_for(connection, question_ids: list[str]) -> dict[str, list[QuestionFigure]]:
    """Diagrams by question id. A question that references a figure it cannot show is
    unanswerable, so these travel with the paper rather than being fetched per question."""
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
                image_url=r["image_url"], placement=r["placement"],
                option_id=r["option_id"], caption=r["caption"],
            )
        )
    return figures


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

    return await _session_payload(connection, session, test)


async def _session_payload(connection, session, test) -> TestSession:
    """A session plus everything a client needs to render or resume it.

    The tenant is read off the session row rather than the caller: a browser client
    holding only a handoff token has no tenant of its own, and the sitting's own
    tenant is the one that decides which paper it is.
    """
    paper = await _paper_for(connection, session["test_id"], session["tenant_id"])

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
                figures=p["figures"],
            )
            for p in paper
        ],
    )


@router.get("/tests/sessions/{session_id}", response_model=TestSession)
async def resume_session(
    session_id: str,
    user: dict | None = Depends(optional_user),
    x_test_token: str | None = Header(default=None),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TestSession:
    session = await _authorized_session(connection, session_id, user, x_test_token)
    test = await connection.fetchrow(
        "SELECT test_id, title, duration_minutes, marks_correct, marks_wrong FROM tests WHERE test_id = $1",
        session["test_id"],
    )
    return await _session_payload(connection, session, test)


@router.post("/tests/handoff/{code}", response_model=TestSession)
async def claim_handoff(
    code: str,
    request: Request,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TestSession:
    """Exchange a handoff code for its sitting, so a paper started in the app can be
    sat in a browser.

    Deliberately NOT signed in. The browser is only ever reachable with a code, and a
    code is only minted by a signed-in student, so the code already carries the proof
    of identity; asking again would re-prove it while the clock runs. What it grants is
    exactly one sitting — no account, no other paper, no history.

    Single use: the code dies on being claimed, so one glimpsed later is already spent.
    The reply carries a separate `web_token`, which the browser sends thereafter.
    """
    _rate_limit_claim(request.client.host if request.client else "unknown")

    session = await connection.fetchrow(
        "SELECT * FROM test_sessions WHERE handoff_code = $1",
        code.strip().upper(),
    )
    if session is None:
        raise HTTPException(status_code=404, detail="That code does not match a test")
    if session["submitted_at"] is not None:
        raise HTTPException(status_code=409, detail="That test has already been submitted")
    if session["expires_at"] <= datetime.now(timezone.utc):
        raise HTTPException(status_code=409, detail="That test's time is up")
    if session["handoff_claimed_at"] is not None and session["web_token"]:
        # Already open in a browser. Hand back the same sitting rather than refusing:
        # a refresh must not lock a student out of their own paper mid-test.
        pass
    else:
        session = await connection.fetchrow(
            """
            UPDATE test_sessions
            SET handoff_claimed_at = now(), web_token = $2
            WHERE session_id = $1 RETURNING *
            """,
            session["session_id"], secrets.token_urlsafe(32),
        )

    test = await connection.fetchrow(
        "SELECT test_id, title, duration_minutes, marks_correct, marks_wrong FROM tests WHERE test_id = $1",
        session["test_id"],
    )
    payload = await _session_payload(connection, session, test)
    return payload.model_copy(update={"web_token": session["web_token"]})


@router.post("/tests/sessions/{session_id}/submit", response_model=TestSessionResult)
async def submit_session(
    session_id: str,
    user: dict | None = Depends(optional_user),
    x_test_token: str | None = Header(default=None),
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
    session = await _authorized_session(connection, session_id, user, x_test_token)

    test = await connection.fetchrow(
        "SELECT test_id, title, marks_correct, marks_wrong FROM tests WHERE test_id = $1",
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
                attempt_id, session["firebase_uid"], session["tenant_id"],
                r["question_id"], session_id,
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
        title=test["title"],
        marks_correct=test["marks_correct"],
        marks_wrong=test["marks_wrong"],
        review=await _review_for(connection, session, updated["submitted_at"]),
    )


@router.get("/tests/sessions/{session_id}/result", response_model=TestSessionResult)
async def session_result(
    session_id: str,
    user: dict | None = Depends(optional_user),
    x_test_token: str | None = Header(default=None),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TestSessionResult:
    """The score card for a sitting that has already been submitted.

    Submission itself returns this, but a browser refresh on the score page would
    otherwise lose it, and re-submitting to get it back reads like a mistake.
    """
    session = await _authorized_session(connection, session_id, user, x_test_token)
    if session["submitted_at"] is None:
        raise HTTPException(status_code=409, detail="This paper has not been submitted yet")

    test = await connection.fetchrow(
        "SELECT title, marks_correct, marks_wrong FROM tests WHERE test_id = $1",
        session["test_id"],
    )
    total = await connection.fetchval(
        "SELECT COUNT(*) FROM test_questions WHERE test_id = $1", session["test_id"]
    )
    return TestSessionResult(
        session_id=session_id,
        test_id=session["test_id"],
        submitted_at=session["submitted_at"],
        total_questions=total or 0,
        correct_count=session["correct_count"] or 0,
        wrong_count=session["wrong_count"] or 0,
        skipped_count=session["skipped_count"] or 0,
        score=float(session["score"] or 0),
        max_score=(total or 0) * test["marks_correct"],
        title=test["title"],
        marks_correct=test["marks_correct"],
        marks_wrong=test["marks_wrong"],
        review=await _review_for(connection, session, session["submitted_at"]),
    )


async def _review_for(connection, session, submitted_at) -> list[QuestionResult]:
    """The paper with the key alongside what the student chose.

    Only called once a sitting is submitted. That is the whole safety argument: before
    submission this data does not leave the server, and afterwards there is nothing
    left to protect.
    """
    if submitted_at is None:
        return []

    rows = await connection.fetch(
        """
        SELECT tq.position, q.question_id, q.question_text, q.options_json,
               q.correct_option_ids, q.explanation_json,
               r.selected_option_ids, a.is_correct
        FROM test_questions tq
        JOIN questions q ON q.question_id = tq.question_id
        LEFT JOIN test_responses r
               ON r.question_id = q.question_id AND r.session_id = $2::uuid
        LEFT JOIN attempts a
               ON a.question_id = q.question_id AND a.session_id = $2::uuid
        WHERE tq.test_id = $1 AND q.tenant_id = $3
        ORDER BY tq.position
        """,
        session["test_id"], str(session["session_id"]), session["tenant_id"],
    )
    figures = await _figures_for(connection, [r["question_id"] for r in rows])

    review = []
    for r in rows:
        chosen = list(r["selected_option_ids"] or [])
        explanation = r["explanation_json"] or {}
        review.append(
            QuestionResult(
                position=r["position"],
                question_id=r["question_id"],
                question_text=r["question_text"],
                options=r["options_json"],
                figures=figures.get(r["question_id"], []),
                selected_option_ids=chosen,
                correct_option_ids=list(r["correct_option_ids"] or []),
                explanation=explanation.get("text") if isinstance(explanation, dict) else None,
                # Unanswered is its own outcome, not a wrong answer: it costs no marks.
                status="skipped" if not chosen else ("correct" if r["is_correct"] else "wrong"),
            )
        )
    return review


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
    user: dict | None = Depends(optional_user),
    x_test_token: str | None = Header(default=None),
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
    session = await _authorized_session(connection, session_id, user, x_test_token)
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
    return await _session_payload(connection, session, test)
