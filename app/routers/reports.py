"""The analytics screen, computed from the attempt log.

Everything here derives from `attempts`: one row per answered question, whether it was
answered in practice or in a mock paper. That the two are the same rows is the point —
a weakness found in a paper and a weakness found in practice are the same weakness, and
the screen should not care which produced it.

Computed live rather than from a precomputed table. At this scale the queries are cheap
and there is no staleness to reason about; a cache can come when the numbers say so.

Every date and hour in the reply is Indian Standard Time. "Today", the streak boundary
and "when do you work best" are all local questions, and every student is in one
country, so the timezone is applied here rather than stored per user.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncpg
from fastapi import APIRouter, Depends, Query

from app.auth import current_tenant, require_user
from app.db import get_connection
from app.schemas import (
    DayStudy,
    DifficultySlice,
    TimeSplit,
    HourAccuracy,
    ReportSummary,
    StreakInfo,
    SubjectSlice,
)

router = APIRouter()

IST = timezone(timedelta(hours=5, minutes=30))

# A day counts towards the streak once this much study time is recorded in it. Chosen
# to measure work rather than presence: opening the app and answering two questions is
# not a day of study, and a bar in minutes cannot be met by tapping quickly.
STREAK_GOAL_MINUTES = 15

_PERIODS = {"today": 1, "week": 7, "month": 30, "all": None}


def _window(period: str) -> tuple[datetime | None, datetime | None]:
    """(start, previous_start) in UTC for a period named in IST days."""
    days = _PERIODS[period]
    if days is None:
        return None, None
    midnight_ist = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight_ist - timedelta(days=days - 1)
    return start.astimezone(timezone.utc), (start - timedelta(days=days)).astimezone(
        timezone.utc
    )


def _accuracy(correct: int, attempted: int) -> float:
    return round(correct / attempted, 4) if attempted else 0.0


def _coverage(solved: int, total: int) -> float:
    return round(solved / total, 4) if total else 0.0


# Questions a student can actually reach, with the subject they belong to. Mirrors the
# scope used by /progress: published questions, reachable through the tree. A question
# with no concept mapping is unreachable in the app and must not count against coverage.
_REACHABLE = """
SELECT DISTINCT q.question_id, chapter.subject_id, q.question_type
  FROM questions q
  JOIN question_concept_mappings qcm ON qcm.question_id = q.question_id
  JOIN nodes concept  ON concept.node_id  = qcm.concept_node_id
  JOIN nodes subtopic ON subtopic.node_id = concept.parent_id
  JOIN nodes topic    ON topic.node_id    = subtopic.parent_id
  JOIN nodes chapter  ON chapter.node_id  = topic.parent_id
 WHERE q.tenant_id = $1 AND q.status = 'published' AND chapter.status = 'published'
"""


_REACHABLE_WITH_DIFFICULTY = _REACHABLE.replace(
    "q.question_type", "q.question_type, q.difficulty"
)


@router.get("/reports", response_model=ReportSummary)
async def get_reports(
    period: str = Query("week", pattern="^(today|week|month|all)$"),
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> ReportSummary:
    """Every number the analytics screen shows, from one read of the attempt log."""
    uid = user["uid"]
    start, previous_start = _window(period)

    totals = await connection.fetchrow(
        f"""
        WITH reachable AS ({_REACHABLE})
        SELECT COUNT(*) AS attempted,
               COUNT(*) FILTER (WHERE a.is_correct) AS correct,
               COUNT(DISTINCT a.question_id) AS distinct_questions,
               COUNT(DISTINCT a.question_id) FILTER (WHERE a.is_correct) AS solved,
               COALESCE(SUM(a.time_spent_ms), 0) AS ms,
               COUNT(*) FILTER (WHERE r.question_type = 'pyq') AS pyq_attempted,
               COUNT(*) FILTER (WHERE r.question_type = 'pyq' AND a.is_correct)
                   AS pyq_correct
          FROM attempts a
          JOIN reachable r ON r.question_id = a.question_id
         WHERE a.firebase_uid = $2 AND ($3::timestamptz IS NULL OR a.created_at >= $3)
        """,
        tenant, uid, start,
    )

    # Coverage is always measured against the whole published bank, never against the
    # period: "how far through the course am I" does not reset every Monday.
    available = await connection.fetchrow(
        f"""
        WITH reachable AS ({_REACHABLE})
        SELECT COUNT(*) AS total,
               COUNT(*) FILTER (WHERE m.ever_correct) AS solved
          FROM reachable r
          LEFT JOIN (
              SELECT question_id, BOOL_OR(is_correct) AS ever_correct
                FROM attempts WHERE firebase_uid = $2 GROUP BY question_id
          ) m ON m.question_id = r.question_id
        """,
        tenant, uid,
    )

    subject_rows = await connection.fetch(
        f"""
        WITH reachable AS ({_REACHABLE}),
        -- Work done in the chosen period: what the student did this week.
        recent AS (
            SELECT question_id,
                   COUNT(*) AS tries,
                   COUNT(*) FILTER (WHERE is_correct) AS hits,
                   COALESCE(SUM(time_spent_ms), 0) AS ms
              FROM attempts
             WHERE firebase_uid = $2 AND ($3::timestamptz IS NULL OR created_at >= $3)
             GROUP BY question_id
        ),
        -- Ground covered, ever. "How far through the course am I" is not a question
        -- that resets every Monday, so coverage ignores the period entirely.
        ever AS (
            SELECT question_id, BOOL_OR(is_correct) AS solved
              FROM attempts WHERE firebase_uid = $2 GROUP BY question_id
        )
        SELECT r.subject_id,
               COUNT(*) AS total_questions,
               COUNT(e.question_id) AS attempted_questions,
               COUNT(e.question_id) FILTER (WHERE e.solved) AS solved_questions,
               COALESCE(SUM(n.tries), 0) AS attempted,
               COALESCE(SUM(n.hits), 0) AS correct,
               COALESCE(SUM(n.ms), 0) AS ms
          FROM reachable r
          LEFT JOIN recent n ON n.question_id = r.question_id
          LEFT JOIN ever   e ON e.question_id = r.question_id
         GROUP BY r.subject_id
         ORDER BY r.subject_id
        """,
        tenant, uid, start,
    )

    titles = {
        r["node_id"]: r["title"]
        for r in await connection.fetch(
            "SELECT node_id, title FROM nodes WHERE type = 'subject' AND tenant_id = $1",
            tenant,
        )
    }

    subjects = [
        SubjectSlice(
            subject_id=r["subject_id"],
            title=titles.get(r["subject_id"], r["subject_id"].title()),
            attempted=r["attempted"],
            correct=r["correct"],
            accuracy=_accuracy(r["correct"], r["attempted"]),
            minutes=round(r["ms"] / 60000),
            total_questions=r["total_questions"],
            attempted_questions=r["attempted_questions"],
            solved_questions=r["solved_questions"],
            coverage=_coverage(r["solved_questions"], r["total_questions"]),
        )
        for r in subject_rows
    ]

    difficulty = await _difficulty(connection, tenant, uid)
    time_split = await _time_split(connection, uid, start)
    by_day = await _study_by_day(connection, uid, days=7)
    by_hour = await _accuracy_by_hour(connection, uid, start)
    streak = await _streak(connection, uid)

    previous_attempted = previous_minutes = None
    if previous_start is not None and start is not None:
        previous = await connection.fetchrow(
            """
            SELECT COUNT(*) AS attempted, COALESCE(SUM(time_spent_ms), 0) AS ms
              FROM attempts
             WHERE firebase_uid = $1 AND created_at >= $2 AND created_at < $3
            """,
            uid, previous_start, start,
        )
        # Only offered when the student was actually here last period. A comparison
        # against a period they did not use reads as a collapse in effort.
        if previous["attempted"] > 0:
            previous_attempted = previous["attempted"]
            previous_minutes = round(previous["ms"] / 60000)

    return ReportSummary(
        period=period,
        since=start,
        attempted=totals["attempted"],
        correct=totals["correct"],
        wrong=totals["attempted"] - totals["correct"],
        accuracy=_accuracy(totals["correct"], totals["attempted"]),
        distinct_questions=totals["distinct_questions"],
        minutes=round(totals["ms"] / 60000),
        pyq_attempted=totals["pyq_attempted"],
        pyq_correct=totals["pyq_correct"],
        total_questions=available["total"],
        solved_questions=available["solved"],
        coverage=_coverage(available["solved"], available["total"]),
        subjects=subjects,
        difficulty=difficulty,
        time_split=time_split,
        by_day=by_day,
        by_hour=by_hour,
        streak=streak,
        previous_attempted=previous_attempted,
        previous_minutes=previous_minutes,
    )


async def _difficulty(connection, tenant: str, uid: str) -> list[DifficultySlice]:
    """The bank split by difficulty, with how far the student has got through each.

    Always measured against the whole bank rather than the period, for the same reason
    coverage is: it answers "how much of the hard material have I done", and that does
    not reset. Questions with no difficulty recorded are left out rather than bundled
    into a bucket they were never assigned to.
    """
    rows = await connection.fetch(
        f"""
        WITH reachable AS ({_REACHABLE_WITH_DIFFICULTY}),
        mine AS (
            SELECT question_id, BOOL_OR(is_correct) AS solved
              FROM attempts WHERE firebase_uid = $2 GROUP BY question_id
        )
        SELECT r.difficulty,
               COUNT(*) AS total,
               COUNT(m.question_id) FILTER (WHERE m.solved) AS solved,
               COUNT(m.question_id) AS attempted
          FROM reachable r
          LEFT JOIN mine m ON m.question_id = r.question_id
         WHERE r.difficulty IS NOT NULL
         GROUP BY r.difficulty
        """,
        tenant, uid,
    )
    order = {"easy": 0, "medium": 1, "hard": 2}
    return sorted(
        (
            DifficultySlice(
                difficulty=r["difficulty"],
                total=r["total"],
                solved=r["solved"],
                attempted=r["attempted"],
            )
            for r in rows
        ),
        key=lambda d: order.get(d.difficulty, 9),
    )


async def _time_split(connection, uid: str, start) -> TimeSplit:
    """Practice against papers. `session_id` is what tells the two apart."""
    row = await connection.fetchrow(
        """
        SELECT COALESCE(SUM(time_spent_ms) FILTER (WHERE session_id IS NULL), 0) AS practice,
               COALESCE(SUM(time_spent_ms) FILTER (WHERE session_id IS NOT NULL), 0) AS test
          FROM attempts
         WHERE firebase_uid = $1 AND ($2::timestamptz IS NULL OR created_at >= $2)
        """,
        uid, start,
    )
    return TimeSplit(
        practice_minutes=round(row["practice"] / 60000),
        test_minutes=round(row["test"] / 60000),
    )


async def _study_by_day(connection, uid: str, days: int) -> list[DayStudy]:
    """The last `days` IST days, including the ones with nothing on them.

    A week chart with missing bars misreads as a week with fewer days in it, so empty
    days are filled rather than omitted.
    """
    rows = await connection.fetch(
        """
        SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date AS day,
               COALESCE(SUM(time_spent_ms), 0) AS ms,
               COUNT(*) AS questions,
               COUNT(*) FILTER (WHERE is_correct) AS correct
          FROM attempts
         WHERE firebase_uid = $1
           AND created_at >= (now() AT TIME ZONE 'Asia/Kolkata')::date - ($2::int - 1)
         GROUP BY 1
        """,
        uid, days,
    )
    found = {r["day"]: r for r in rows}
    today = datetime.now(IST).date()

    out: list[DayStudy] = []
    for offset in range(days - 1, -1, -1):
        day = today - timedelta(days=offset)
        row = found.get(day)
        out.append(
            DayStudy(
                day=day,
                label=day.strftime("%a"),
                minutes=round((row["ms"] if row else 0) / 60000),
                questions=row["questions"] if row else 0,
                correct=row["correct"] if row else 0,
                accuracy=_accuracy(row["correct"], row["questions"]) if row else 0.0,
            )
        )
    return out


async def _accuracy_by_hour(connection, uid: str, start) -> list[HourAccuracy]:
    """Accuracy per hour of the IST clock. Only hours actually studied are returned."""
    rows = await connection.fetch(
        """
        SELECT EXTRACT(HOUR FROM created_at AT TIME ZONE 'Asia/Kolkata')::int AS hour,
               COUNT(*) AS attempted,
               COUNT(*) FILTER (WHERE is_correct) AS correct
          FROM attempts
         WHERE firebase_uid = $1 AND ($2::timestamptz IS NULL OR created_at >= $2)
         GROUP BY 1 ORDER BY 1
        """,
        uid, start,
    )
    return [
        HourAccuracy(
            hour=r["hour"],
            attempted=r["attempted"],
            correct=r["correct"],
            accuracy=_accuracy(r["correct"], r["attempted"]),
        )
        for r in rows
    ]


async def _streak(connection, uid: str) -> StreakInfo:
    """Consecutive IST days meeting the daily minutes bar, counting back from today.

    Today not being met does not break the streak — the day is not over. It breaks on
    the first *finished* day that fell short, which is why the walk starts at yesterday
    when today has not been earned yet.
    """
    rows = await connection.fetch(
        """
        SELECT (created_at AT TIME ZONE 'Asia/Kolkata')::date AS day,
               COALESCE(SUM(time_spent_ms), 0) AS ms
          FROM attempts WHERE firebase_uid = $1
         GROUP BY 1 ORDER BY 1
        """,
        uid,
    )
    goal_ms = STREAK_GOAL_MINUTES * 60000
    earned = sorted(r["day"] for r in rows if r["ms"] >= goal_ms)
    today = datetime.now(IST).date()
    minutes_today = round(
        next((r["ms"] for r in rows if r["day"] == today), 0) / 60000
    )
    met_today = today in earned

    longest = run = 0
    for index, day in enumerate(earned):
        run = run + 1 if index and (day - earned[index - 1]).days == 1 else 1
        longest = max(longest, run)

    current = 0
    cursor = today if met_today else today - timedelta(days=1)
    earned_set = set(earned)
    while cursor in earned_set:
        current += 1
        cursor -= timedelta(days=1)

    return StreakInfo(
        current=current,
        longest=longest,
        minutes_today=minutes_today,
        goal_minutes=STREAK_GOAL_MINUTES,
        met_today=met_today,
    )
