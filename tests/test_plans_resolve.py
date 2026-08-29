"""Selector resolution, freezing, and whether a step is done.

Two things are being guarded here and they are not the same. Resolution decides *which*
questions a step holds, and it must be frozen so that "6 of 8 right" keeps meaning the
same eight. Progress decides whether the student has passed, and it must come out of the
attempt log rather than a tap, because the plan's whole claim is that finishing it means
something.

The progress arithmetic is pure and tested directly. Resolution needs a connection, so it
runs against a fake that records the SQL — enough to prove the filters, the ordering and
the freeze race are what they claim to be, without a database.
"""

import asyncio

import pytest

from app.plans.progress import (
    StepProgress,
    _state_for,
    plan_is_complete,
    plan_percent,
    step_progress,
)
from app.plans.resolve import (
    _ORDERINGS,
    _resolution_query,
    freeze_item,
    resolve_selector,
    selector_from_row,
)
from app.plans.schema import QuestionSelector


class _FakeConnection:
    """Records what was asked, answers with what it was given."""

    def __init__(self, rows=(), values=None):
        self.rows = list(rows)
        self.values = list(values or [])
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return self.values.pop(0) if self.values else None


def _selector(**kw):
    base = dict(concept_node_ids=["c_height"], count=8)
    base.update(kw)
    return QuestionSelector(**base)


def _item(**kw):
    base = dict(
        item_id="11111111-1111-1111-1111-111111111111",
        item_type="questions",
        sel_concept_ids=["c_height"],
        sel_types=["pyq"],
        sel_difficulty=["medium"],
        sel_count=8,
        sel_order="mixed",
        sel_exclude_seen=False,
        resolved_question_ids=None,
    )
    base.update(kw)
    return base


# --- what the selector asks for -----------------------------------------------------


def test_a_selector_expands_a_subtopic_to_its_concepts():
    """A rolled-up inventory counts in subtopics, so a plan written from one names them.

    Without the expansion, every selector in a large chapter's plan would resolve to
    nothing: `question_concept_mappings` is keyed by concept and a subtopic id matches no
    row in it.
    """
    connection = _FakeConnection()
    asyncio.run(resolve_selector(connection, "JEENE_MASTER", "uid", _selector()))
    query = connection.calls[0][0]
    assert "WITH RECURSIVE targets" in query
    assert "WHERE type = 'concept'" in query


def test_unrated_is_translated_to_a_null_check_not_compared_as_a_string():
    """`unrated` is the planner's word for an ungraded question. The column holds NULL."""
    connection = _FakeConnection()
    asyncio.run(
        resolve_selector(
            connection, "JEENE_MASTER", "uid", _selector(difficulty=["medium", "unrated"])
        )
    )
    query, args = connection.calls[0]
    assert "q.difficulty IS NULL" in query
    assert args[4] == ["medium"], "unrated must not reach the column comparison"
    assert args[5] is True, "and must set the IS NULL flag instead"


def test_an_unrated_only_selector_asks_only_for_ungraded_questions():
    """An empty array and NULL are different questions, and this once conflated them.

    `[d for d in difficulty if d != "unrated"] or None` turned "match no graded
    difficulty" into "do not filter on difficulty", so an unrated-only selector drew from
    the whole bank. The unit test asserted `None` and called it correct; a real database
    found it in one query. Empty array here, and it must stay empty.
    """
    connection = _FakeConnection()
    asyncio.run(
        resolve_selector(connection, "JEENE_MASTER", "uid", _selector(difficulty=["unrated"]))
    )
    args = connection.calls[0][1]
    assert args[4] == [], "not None — NULL would disable the filter entirely"
    assert args[5] is True


def test_no_difficulty_at_all_is_the_only_thing_that_disables_the_filter():
    connection = _FakeConnection()
    asyncio.run(resolve_selector(connection, "JEENE_MASTER", "uid", _selector()))
    assert connection.calls[0][1][4] is None


@pytest.mark.parametrize(
    "difficulty,graded,unrated",
    [
        ([], None, False),
        (["easy"], ["easy"], False),
        (["unrated"], [], True),
        (["easy", "unrated"], ["easy"], True),
    ],
)
def test_the_difficulty_filter_distinguishes_none_from_empty(difficulty, graded, unrated):
    from app.plans.resolve import difficulty_filter

    assert difficulty_filter(difficulty) == (graded, unrated)


def test_empty_filters_mean_any_rather_than_none():
    """A scope whose questions are all one type must not need the planner to enumerate."""
    connection = _FakeConnection()
    asyncio.run(resolve_selector(connection, "JEENE_MASTER", "uid", _selector()))
    args = connection.calls[0][1]
    assert args[3] is None, "no type filter"
    assert args[4] is None, "no difficulty filter"
    assert args[5] is False


def test_a_selector_with_no_nodes_or_no_count_runs_no_query_at_all():
    connection = _FakeConnection()
    assert asyncio.run(
        resolve_selector(connection, "JEENE_MASTER", "uid", _selector(concept_node_ids=[]))
    ) == []
    assert asyncio.run(
        resolve_selector(connection, "JEENE_MASTER", "uid", _selector(count=0))
    ) == []
    assert connection.calls == []


# --- ordering ------------------------------------------------------------------------


@pytest.mark.parametrize("order", sorted(_ORDERINGS))
@pytest.mark.parametrize("exclude_seen", [False, True])
def test_every_query_shape_passes_exactly_as_many_arguments_as_it_has_placeholders(
    order, exclude_seen
):
    """asyncpg counts them, so a mismatch is a runtime failure, not a wrong result.

    This existed: the salt placeholder is only rendered for one of the three orderings,
    but the caller passed a fixed list of eight arguments — so two thirds of the
    orderings would have failed the moment they met a real connection. A fake connection
    never counts, which is exactly why this is asserted against the rendered SQL.
    """
    import re

    selector = _selector(order=order, exclude_seen=exclude_seen)
    sql, args = _resolution_query(selector, "JEENE_MASTER", "uid", "salt")
    highest = max(int(n) for n in re.findall(r"\$(\d+)", sql))
    assert highest == len(args), f"{order}: {highest} placeholders, {len(args)} args"


def test_the_mixed_deck_is_salted_so_one_slice_does_not_serve_everyone():
    """Unsalted, a forty-question bank would have the same eight doing all the work."""
    sql, args = _resolution_query(_selector(order="mixed"), "T", "uid", "item-1")
    assert "md5($8 || q.question_id)" in sql
    assert args[-1] == "item-1"


def test_mixed_is_deterministic_rather_than_random():
    """Two people debugging the same step must see the same questions.

    `random()` would also mean a re-resolve after a content edit churns the whole deck.
    """
    assert "random" not in _ORDERINGS["mixed"].lower()
    assert "md5" in _ORDERINGS["mixed"]


def test_the_two_graded_orderings_are_opposites():
    assert "ASC" in _ORDERINGS["easiest_first"]
    assert "DESC" in _ORDERINGS["hardest_first"]


def test_only_orderings_from_this_module_can_reach_an_order_by():
    """Nothing a client or a model wrote is interpolated into the query."""
    for clause in _ORDERINGS.values():
        assert ";" not in clause and "--" not in clause


def test_excluding_seen_questions_tops_up_with_the_oldest_rather_than_giving_up():
    """A checkpoint that asked for ten in a scope with six unseen should still hand over
    ten — the four it tops up with are the ones most likely to have been forgotten."""
    connection = _FakeConnection()
    asyncio.run(
        resolve_selector(
            connection, "JEENE_MASTER", "uid", _selector(exclude_seen=True)
        )
    )
    order = connection.calls[0][0].split("ORDER BY")[1]
    assert "(last_seen IS NOT NULL)" in order, "unseen first"
    assert "last_seen ASC" in order, "then longest ago"


def test_not_excluding_seen_leaves_the_ordering_alone():
    connection = _FakeConnection()
    asyncio.run(resolve_selector(connection, "JEENE_MASTER", "uid", _selector()))
    assert "last_seen" not in connection.calls[0][0].split("ORDER BY")[1]


# --- freezing -------------------------------------------------------------------------


def test_an_item_that_is_already_frozen_is_not_resolved_again():
    """The whole point. A deck that reshuffles makes '6 of 8 right' meaningless."""
    connection = _FakeConnection()
    frozen = ["q1", "q2", "q3"]
    got = asyncio.run(
        freeze_item(connection, "JEENE_MASTER", "uid", _item(resolved_question_ids=frozen))
    )
    assert got == frozen
    assert connection.calls == [], "a frozen item must not touch the database"


def test_freezing_writes_the_resolved_ids_back_exactly_once():
    connection = _FakeConnection(
        rows=[{"question_id": "q1"}, {"question_id": "q2"}],
        values=[["q1", "q2"]],
    )
    got = asyncio.run(freeze_item(connection, "JEENE_MASTER", "uid", _item()))
    assert got == ["q1", "q2"]
    update = connection.calls[1][0]
    assert "UPDATE study_plan_step_items" in update
    assert "resolved_at IS NULL" in update, "the write must be conditional"


def test_losing_the_freeze_race_returns_the_winners_deck_not_your_own():
    """Two devices opening the same step must not end up with different questions."""
    connection = _FakeConnection(
        rows=[{"question_id": "mine1"}, {"question_id": "mine2"}],
        # The conditional UPDATE matches nothing: somebody else got there first.
        values=[None, ["theirs1", "theirs2"]],
    )
    got = asyncio.run(freeze_item(connection, "JEENE_MASTER", "uid", _item()))
    assert got == ["theirs1", "theirs2"]


def test_a_selector_that_finds_nothing_is_not_frozen_empty():
    """A scope unpublished for a moment must not permanently empty a step."""
    connection = _FakeConnection(rows=[])
    got = asyncio.run(freeze_item(connection, "JEENE_MASTER", "uid", _item()))
    assert got == []
    assert not any("UPDATE" in call[0] for call in connection.calls)


def test_under_supply_freezes_what_exists_rather_than_failing():
    """A step that asked for eight and found three shows three."""
    connection = _FakeConnection(
        rows=[{"question_id": f"q{i}"} for i in range(3)],
        values=[["q0", "q1", "q2"]],
    )
    got = asyncio.run(freeze_item(connection, "JEENE_MASTER", "uid", _item(sel_count=8)))
    assert got == ["q0", "q1", "q2"]


def test_the_shortfall_is_readable_from_the_row_without_another_column():
    """`sel_count` is what was asked for; the frozen array is what exists.

    Any client that renders the array length is telling the truth, which is why there is
    no third column to keep in step with the other two.
    """
    item = _item(sel_count=10, resolved_question_ids=["q1", "q2", "q3"])
    assert item["sel_count"] - len(item["resolved_question_ids"]) == 7


def test_a_named_reference_has_no_selector_to_resolve():
    for item_type in ("video", "notes", "test"):
        assert selector_from_row(_item(item_type=item_type, sel_count=None)) is None


def test_a_selector_rebuilds_from_its_columns():
    selector = selector_from_row(_item(sel_order="easiest_first", sel_exclude_seen=True))
    assert selector.concept_node_ids == ["c_height"]
    assert selector.question_types == ["pyq"]
    assert selector.difficulty == ["medium"]
    assert selector.count == 8
    assert selector.order == "easiest_first"
    assert selector.exclude_seen is True


# --- is the step done -----------------------------------------------------------------


@pytest.mark.parametrize(
    "answered,correct,need_q,need_acc,expected",
    [
        (0, 0, 6, 0.6, "pending"),        # not started
        (4, 4, 6, 0.6, "in_progress"),    # perfect but not enough of them
        (8, 4, 6, 0.6, "in_progress"),    # enough answered, not well enough
        (6, 4, 6, 0.6, "done"),           # exactly on both bars
        (8, 8, 6, 0.6, "done"),
    ],
)
def test_a_step_needs_both_bars(answered, correct, need_q, need_acc, expected):
    """Either bar alone is gameable: answer everything badly, or answer one and stop."""
    assert _state_for(
        answered=answered,
        correct=correct,
        required_questions=need_q,
        required_accuracy=need_acc,
    ) == expected


def test_a_graded_step_with_no_bar_is_unfinishable_rather_than_finished():
    """Unreachable through the tables, but 'no bar' must never read as 'passed'."""
    assert _state_for(
        answered=10, correct=10, required_questions=None, required_accuracy=None
    ) == "in_progress"


def _step(**kw):
    base = dict(
        state="pending",
        completion_kind="accuracy",
        required_questions=3,
        required_accuracy=0.6,
    )
    base.update(kw)
    return base


def test_wrong_then_right_counts_as_right():
    """A question got wrong in March and right in April is known.

    Counting every attempt would mean a student who revised until they understood scores
    worse than one who guessed right first time.
    """
    connection = _FakeConnection(
        rows=[
            {"question_id": "q1", "is_correct": True},
            {"question_id": "q2", "is_correct": True},
            {"question_id": "q3", "is_correct": True},
        ]
    )
    progress = asyncio.run(
        step_progress(connection, "JEENE_MASTER", "uid", _step(), ["q1", "q2", "q3"])
    )
    assert "DISTINCT ON (a.question_id)" in connection.calls[0][0]
    assert "ORDER BY a.question_id, a.created_at DESC" in connection.calls[0][0]
    assert progress.state == "done"
    assert progress.correct == 3


def test_a_self_marked_step_is_whatever_the_student_said():
    connection = _FakeConnection()
    progress = asyncio.run(
        step_progress(
            connection,
            "JEENE_MASTER",
            "uid",
            _step(completion_kind="self", state="done"),
            [],
        )
    )
    assert progress.state == "done"
    assert connection.calls == [], "nothing to derive, so nothing to query"


def test_a_skipped_step_stays_skipped_whatever_the_log_says():
    connection = _FakeConnection()
    progress = asyncio.run(
        step_progress(
            connection, "JEENE_MASTER", "uid", _step(state="skipped"), ["q1", "q2"]
        )
    )
    assert progress.state == "skipped"
    assert connection.calls == []


def test_a_graded_step_whose_selector_found_nothing_is_not_complete():
    connection = _FakeConnection()
    progress = asyncio.run(
        step_progress(connection, "JEENE_MASTER", "uid", _step(), [])
    )
    assert progress.state == "pending"
    assert progress.offered == 0


def test_accuracy_survives_an_unanswered_step():
    assert StepProgress(offered=8, answered=0, correct=0, state="pending").accuracy == 0.0


# --- how far through the plan ---------------------------------------------------------


def test_skipped_steps_leave_the_sum_rather_than_counting_as_done():
    """Skipping four of six has not finished two thirds of anything."""
    assert plan_percent(["done", "skipped", "skipped", "skipped", "skipped", "pending"]) == 0.5
    assert plan_percent(["done", "done", "pending", "pending"]) == 0.5


def test_a_plan_of_nothing_but_skips_is_not_finished():
    """A completion on a student's history they would not recognise is worse than none."""
    assert plan_is_complete(["skipped", "skipped"]) is False
    assert plan_percent(["skipped", "skipped"]) == 0.0


def test_a_plan_completes_when_every_step_that_counts_is_done():
    assert plan_is_complete(["done", "done", "skipped"]) is True
    assert plan_is_complete(["done", "in_progress"]) is False
    assert plan_percent(["done", "done", "skipped"]) == 1.0


# --- the widened endpoint --------------------------------------------------------------


def test_the_question_page_filters_are_shared_by_the_count_and_the_rows():
    """A total that disagrees with the rows underneath it is worse than no total."""
    import inspect

    from app.routers.content import _paginated_questions_for_node_ids

    source = inspect.getsource(_paginated_questions_for_node_ids)
    assert source.count("+ _NOT_UNRELEASED_TEST + filters") >= 1
    assert source.count("filters") >= 3, "one filter string, used by both queries"


def test_the_endpoint_rejects_a_difficulty_it_does_not_know():
    from app.routers.content import _DIFFICULTIES

    assert _DIFFICULTIES == {"easy", "medium", "hard", "unrated"}
