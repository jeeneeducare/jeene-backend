from datetime import datetime, timedelta, timezone

from app.routers.mistakes import _assemble, _wilson_lower_bound, _worth_doing

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)

SUBJECTS = {"phy": "Physics", "bio": "Biology"}
CONCEPTS = {"t_elastic": ["c1", "c2", "c3"], "t_cells": ["c9"]}


class Row(dict):
    """asyncpg.Record stand-in: _assemble only reads by key."""


def answer(qid, *, correct, topic="t_elastic", subject="phy", test=False, ago=0):
    return Row(
        question_id=qid,
        is_correct=correct,
        in_a_test=test,
        created_at=NOW - timedelta(days=ago),
        topic_id=topic,
        topic_title="Elasticity" if topic == "t_elastic" else "Cell Structure",
        chapter_id="phy_11_ch8" if topic == "t_elastic" else "bio_11_ch1",
        chapter_title="Mechanical Properties of Solids",
        subject_id=subject,
    )


def book(rows, ever_wrong=None):
    ever = ever_wrong if ever_wrong is not None else {
        r["question_id"] for r in rows if not r["is_correct"]
    }
    return _assemble(rows, ever, CONCEPTS, SUBJECTS)


# --- the ranking rule ------------------------------------------------------------

def test_one_miss_out_of_one_ranks_below_a_real_pattern():
    """The whole reason the score is not just accuracy.

    A single careless slip in a topic tried once looks like 0% accuracy. If that
    outranked eight misses out of ten, the top of the screen would be noise.
    """
    assert _wilson_lower_bound(1, 1) < _wilson_lower_bound(8, 10)


def test_more_evidence_of_the_same_rate_ranks_higher():
    assert _wilson_lower_bound(2, 4) < _wilson_lower_bound(20, 40)


def test_no_attempts_scores_zero():
    assert _wilson_lower_bound(0, 0) == 0.0


def test_a_bigger_gap_outranks_a_more_certain_but_tiny_one():
    """Confidence alone is not the question.

    Two wrong out of two is more certain than ten out of fifteen, and less worth doing.
    A "start here" list that opens with a topic holding two questions, above one holding
    ten, is answering the wrong question.
    """
    assert _wilson_lower_bound(2, 2) > _wilson_lower_bound(10, 15)   # more certain
    assert _worth_doing(2, 2) < _worth_doing(10, 15)                 # less worth doing


def test_size_cannot_take_over_the_ranking_completely():
    """The square root is what stops one huge topic burying a flat failure.

    Sixteen outstanding at half-right must not outrank three out of three wrong by so
    much that the second never appears.
    """
    assert _worth_doing(3, 3) > _worth_doing(2, 4)


def test_ranking_puts_the_better_evidenced_weakness_first():
    rows = [answer(f"slip{i}", correct=True) for i in range(0)]
    rows += [answer("slip1", correct=False, topic="t_cells", subject="bio")]
    rows += [answer(f"e{i}", correct=i >= 8) for i in range(10)]
    ranked = book(rows).topics
    assert [t.topic_id for t in ranked] == ["t_elastic", "t_cells"]


# --- latest attempt wins ---------------------------------------------------------

def test_a_question_since_got_right_is_fixed_not_outstanding():
    """The row given is the latest attempt; `ever_wrong` remembers the history."""
    result = book([answer("q1", correct=True)], ever_wrong={"q1"})
    assert result.ever_wrong == 1
    assert result.unfixed == 0
    assert result.fixed == 1
    assert result.topics == []       # nothing left to do here, so nothing to show


def test_a_topic_with_nothing_outstanding_is_not_listed():
    result = book([answer("q1", correct=True), answer("q2", correct=True)])
    assert result.topics == []
    assert result.topics_affected == 0


# --- the practice and test split -------------------------------------------------

def test_practice_and_test_are_counted_apart_but_ranked_together():
    rows = [
        answer("q1", correct=False, test=False),
        answer("q2", correct=False, test=True),
        answer("q3", correct=True, test=True),
    ]
    result = book(rows)
    assert (result.practice_wrong, result.test_wrong) == (1, 1)
    topic = result.topics[0]
    assert (topic.practice_wrong, topic.test_wrong) == (1, 1)
    assert topic.unfixed == 2        # one weakness, not two


# --- what the screen leads with --------------------------------------------------

def test_counts_and_accuracy():
    rows = [answer(f"q{i}", correct=i < 7) for i in range(10)]
    result = book(rows)
    assert result.attempted == 10
    assert result.unfixed == 3
    assert result.accuracy == 0.7


def test_topic_carries_where_to_go_and_what_to_practise():
    topic = book([answer("q1", correct=False)]).topics[0]
    assert topic.chapter_id == "phy_11_ch8"
    assert topic.concept_ids == ["c1", "c2", "c3"]
    assert topic.subject_title == "Physics"


def test_last_wrong_at_is_the_most_recent_miss():
    rows = [answer("q1", correct=False, ago=9), answer("q2", correct=False, ago=2)]
    assert book(rows).topics[0].last_wrong_at == NOW - timedelta(days=2)


def test_subjects_are_ordered_by_how_much_is_wrong():
    rows = [answer("q1", correct=False, topic="t_cells", subject="bio")]
    rows += [answer(f"p{i}", correct=False) for i in range(3)]
    assert [s.subject_id for s in book(rows).subjects] == ["phy", "bio"]


# --- the state every new student is in -------------------------------------------

def test_a_student_who_has_answered_nothing_gets_an_empty_book_not_a_crash():
    result = book([])
    assert (result.attempted, result.unfixed, result.accuracy) == (0, 0, 0.0)
    assert result.topics == [] and result.subjects == []


def test_a_student_who_has_never_been_wrong_gets_an_empty_book():
    result = book([answer("q1", correct=True), answer("q2", correct=True)])
    assert result.unfixed == 0 and result.accuracy == 1.0
    assert result.topics == []
