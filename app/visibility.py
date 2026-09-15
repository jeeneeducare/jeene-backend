"""Which questions a student is allowed to meet at all.

Separate from `figures.py`, which decides which *parts* of a question may be shown.
This decides whether the question exists for this reader in the first place.

Two rules, and both are correctness rules:

A question that arrived with a test paper must not appear in ordinary browsing until
that paper is released, or a student can sit the test in the practice deck the week
before. Extracted here when the study planner became the second reader that has to
honour it — a second copy of five lines of SQL is how the figure-placement rule got out
of step with itself, and that cost a leaked answer.

And a past-year question belongs to the exam it was asked in. A NEET student practising
a chapter must not be handed a JEE Main question, which asks things NEET does not and
is scored against a different paper. The bank has always recorded which exam a question
came from, in `questions.pyq_exam`, and until this existed nothing ever read it: every
reader filtered on tenant, status and concept, so publishing a JEE question put it
straight into NEET decks with no way to tell it apart.
"""

from __future__ import annotations

import asyncpg

# Appended to a WHERE clause over `questions q`.
#
# A generated paper draws on questions that already live in the bank, and putting one
# of those into an unreleased test must not pull it out of practice — only questions
# that ARRIVED with a paper are gated.
NOT_UNRELEASED_TEST_SQL = """
  AND (q.source <> 'test_paper' OR EXISTS (
        SELECT 1 FROM test_questions tq
        JOIN tests t ON t.test_id = tq.test_id
        WHERE tq.question_id = q.question_id AND t.released_at IS NOT NULL))
"""


def _track_sql(column: str) -> str:
    """The exam family a free-text exam label belongs to.

    `pyq_exam` is free text written by the content pipeline over several years, and it
    shows: NEET, neet, aipmt, AIPMT, cbse_aipmt, CBSE-AIPMT, "NEET 2016 Phase I", and
    now "JEE Main". Comparing those strings to a student's `target_exam` of 'neet' with
    `=` would have matched two of them and hidden the other forty-six, so the comparison
    is made on the family rather than the label.

    AIPMT and CBSE-AIPMT are NEET: the same paper under its former names, and the
    questions are the ones NEET students revise. Anything unrecognised keeps its own
    lowercased label, so a family nobody has taught this function about is shown only to
    a student who asked for exactly it, rather than leaking to everyone.
    """
    return f"""
        CASE
            WHEN {column} IS NULL OR btrim({column}) = '' THEN NULL
            WHEN lower({column}) LIKE '%jee%' THEN 'jee'
            WHEN lower({column}) LIKE '%neet%'
              OR lower({column}) LIKE '%aipmt%'
              OR lower({column}) LIKE '%pmt%'  THEN 'neet'
            ELSE lower(btrim({column}))
        END
    """


def exam_scope_sql(param: int) -> str:
    """Appended to a WHERE clause over `questions q`. `param` is the $n holding the track.

    A question with no exam on it is not a past-year question and belongs to everybody;
    that is most of the bank, so the common case stays unfiltered. A question that does
    carry an exam is shown only to a student on that track.

    A NULL track disables the rule. Callers are expected to pass a resolved track rather
    than None — `resolve_exam_track` falls back to the tenant's own exam precisely so a
    signed-out reader is still scoped — but the guard is here so that a caller which
    genuinely cannot know (a backfill, an admin export) degrades to today's behaviour
    instead of silently returning nothing.
    """
    return f"""
  AND (${param}::text IS NULL OR {_track_sql('q.pyq_exam')} IS NULL
       OR {_track_sql('q.pyq_exam')} = ${param}::text)
"""


async def resolve_exam_track(
    connection: asyncpg.Connection, tenant: str, uid: str | None
) -> str | None:
    """Which exam family this reader is studying for.

    The student's own `target_exam` when they have one. When they do not — signed out,
    or part-way through onboarding — the catalogue's exam, which is the honest default
    for a single-exam product and keeps every NEET past-year question visible to a
    reader who has not signed in yet. Only an empty `exams` table yields None, and that
    turns the rule off rather than emptying the deck.

    `tenant` is taken and unused: `exams` is not tenant-scoped today, and the argument
    is here so the call sites do not have to change on the day it becomes so.
    """
    if uid:
        track = await connection.fetchval(
            f"SELECT {_track_sql('target_exam')} FROM users WHERE firebase_uid = $1",
            uid,
        )
        if track:
            return track
    return await connection.fetchval(
        f"SELECT {_track_sql('exam_id')} FROM exams ORDER BY exam_id LIMIT 1"
    )
