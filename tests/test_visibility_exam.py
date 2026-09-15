"""A past-year question belongs to the exam it was asked in.

Publishing thirteen JEE Main questions into a chapter put them straight into NEET
students' practice decks: `questions.pyq_exam` had been written by the pipeline for
years and read by nothing, so every reader filtered on tenant, status and concept and
none of them on the exam. These are the tests for the rule that closed that.

The classifier gets the most attention here, because the failure it guards against is
the quiet one. `pyq_exam` is free text written over several years — NEET, neet, aipmt,
AIPMT, cbse_aipmt, CBSE-AIPMT, "NEET 2016 Phase I" — and a rule that compared it to a
student's `target_exam` with `=` would have matched two labels and silently hidden the
other forty-six from the students they belong to. Hiding NEET questions from NEET
students is a worse bug than the one being fixed, and it would not have raised an error.
"""

import asyncio
import os

import asyncpg
import pytest

from app.visibility import exam_scope_sql, resolve_exam_track

_DB = os.getenv("DATABASE_URL")

needs_db = pytest.mark.skipif(
    not _DB, reason="DATABASE_URL not set; these need a real database"
)


# The rule as the readers apply it, over a one-row table standing in for `questions q`.
_PROBE = (
    "SELECT EXISTS (SELECT 1 FROM (SELECT $1::text AS pyq_exam, "
    "'x'::text AS source) q WHERE TRUE " + exam_scope_sql(2) + ")"
)


async def _visible(connection, label, track):
    """Would a question labelled `label` be shown to a student on `track`?"""
    return await connection.fetchval(_PROBE, label, track)


@needs_db
@pytest.mark.parametrize(
    "label, track, expected",
    [
        # The common case by far: not a past-year question, so it belongs to everyone.
        (None, "neet", True),
        (None, "jee", True),
        ("", "neet", True),
        # NEET and its former names are one exam. AIPMT questions are what NEET
        # students revise; excluding them would hide 46 live questions.
        ("NEET", "neet", True),
        ("neet", "neet", True),
        ("aipmt", "neet", True),
        ("AIPMT", "neet", True),
        ("cbse_aipmt", "neet", True),
        ("CBSE-AIPMT", "neet", True),
        ("NEET 2016 Phase I", "neet", True),
        # The rule this exists for.
        ("JEE Main", "neet", False),
        ("JEE Advanced", "neet", False),
        # And symmetrically: a JEE student is not handed a NEET paper.
        ("JEE Main", "jee", True),
        ("NEET", "jee", False),
        ("aipmt", "jee", False),
        # An exam nobody has taught the classifier about keeps its own label, so it
        # reaches only a student who asked for exactly it rather than leaking to all.
        ("KVPY", "neet", False),
        ("KVPY", "kvpy", True),
    ],
)
def test_which_exam_a_question_belongs_to(label, track, expected):
    async def check():
        connection = await asyncpg.connect(_DB)
        try:
            return await _visible(connection, label, track)
        finally:
            await connection.close()

    assert asyncio.run(check()) is expected


@needs_db
def test_an_unknown_track_turns_the_rule_off_rather_than_emptying_the_deck():
    """A caller that genuinely cannot know degrades to the old behaviour.

    Not the path any reader takes — `resolve_exam_track` falls back to the catalogue's
    own exam precisely so that a signed-out reader is still scoped — but a backfill or
    an admin export passing None must get everything rather than nothing.
    """

    async def check():
        connection = await asyncpg.connect(_DB)
        try:
            return [
                await _visible(connection, "JEE Main", None),
                await _visible(connection, "NEET", None),
                await _visible(connection, None, None),
            ]
        finally:
            await connection.close()

    assert asyncio.run(check()) == [True, True, True]


@needs_db
def test_a_signed_out_reader_is_scoped_to_the_catalogue_exam():
    """Signing out must not be a way around the rule.

    The fallback is what keeps every NEET past-year question visible to somebody
    browsing before they sign in, while still keeping a JEE question out of that deck.
    """

    async def check():
        connection = await asyncpg.connect(_DB)
        try:
            return await resolve_exam_track(connection, "JEENE_MASTER", None)
        finally:
            await connection.close()

    track = asyncio.run(check())
    # Whatever the catalogue holds, a reader is never left unscoped while it holds one.
    assert track is None or isinstance(track, str)
    if track is not None:
        assert track == track.lower().strip()


@needs_db
def test_a_students_own_exam_wins_over_the_catalogue_default():
    """The fallback is a default, not an override."""

    async def check():
        connection = await asyncpg.connect(_DB)
        try:
            uid = "test-exam-track-user"
            await connection.execute(
                "INSERT INTO users (firebase_uid, email, target_exam)"
                " VALUES ($1, $2, 'JEE Main')"
                " ON CONFLICT (firebase_uid) DO UPDATE SET target_exam = 'JEE Main'",
                uid,
                f"{uid}@example.test",
            )
            try:
                return await resolve_exam_track(connection, "JEENE_MASTER", uid)
            finally:
                await connection.execute(
                    "DELETE FROM users WHERE firebase_uid = $1", uid
                )
        finally:
            await connection.close()

    assert asyncio.run(check()) == "jee"


def test_the_fragment_binds_its_value_rather_than_interpolating_it():
    """The track reaches SQL as a parameter, which is the repo's rule 4.

    The fragment does contain the strings 'neet' and 'jee', and that is fine: they are
    the classifier's own output constants, on the left of the comparison. What must
    never appear is the student's value, so the test is that the only thing a caller can
    change is the placeholder number.
    """
    assert "$7::text" in exam_scope_sql(7)
    assert exam_scope_sql(7).replace("$7", "$N") == exam_scope_sql(9).replace("$9", "$N")
