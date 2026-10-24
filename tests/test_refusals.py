"""Refusals the apps are allowed to show as written.

The bug these exist for: a student with three plans open tapped "Build my plan" and read
"Something changed while you were here. Try again." Nothing had changed, and trying again
would have failed every time until a plan was archived. The server had written the right
sentence — archive one first, and which one — and the app threw it away, because it maps
a status code to a line of its own and keeps `detail` for the logs.

That rule is right for most of this API and wrong for these, so the refusal now says who
it is addressed to. What is tested here is the saying: that every student-facing refusal
carries a sentence, that it is distinguishable from a `402` gate body, and that nothing
which is *not* addressed to a student acquires an audience by accident.
"""

import asyncio

import pytest

from app.plans import store
from app.refusals import (
    ACTION_HEADER,
    ARCHIVE_PLAN,
    AUDIENCE_HEADER,
    STUDENT,
    refuse,
)
from app.routers import plans as plans_module
from tests.test_plans_api import _FakeConnection


def test_a_refusal_names_its_audience():
    """The header the apps switch on. Without it this is an ordinary detail string."""
    exc = refuse(409, "You already have 3 plans on the go.", action=ARCHIVE_PLAN)
    assert exc.status_code == 409
    assert exc.detail == "You already have 3 plans on the go."
    assert exc.headers[AUDIENCE_HEADER] == STUDENT
    assert exc.headers[ACTION_HEADER] == ARCHIVE_PLAN


def test_an_action_is_optional_and_absent_rather_than_empty():
    """An app reads the action to offer a button. A blank header is not a button."""
    assert ACTION_HEADER not in refuse(409, "Nothing to plan from yet.").headers


def test_the_body_stays_the_shape_every_installed_app_already_reads():
    """The reason this is a header at all, asserted rather than only written down.

    An object in `detail` decodes cleanly into the app's `GateBlock` — every field has a
    default and unknown keys are ignored — so a build that predates this would have read
    a shelf limit as a paywall and offered Pro to somebody already paying. A plain string
    is what those builds have always handled, so they keep showing their own line for the
    status and nothing regresses.
    """
    detail = refuse(409, "Archive one first", action=ARCHIVE_PLAN).detail
    assert isinstance(detail, str)


def test_the_audience_is_not_something_a_caller_sets_by_accident():
    """`refuse` is the only way in, and it always marks the sentence as the student's.

    Guards the direction of the mistake that matters: a detail meant for a log must not
    be able to acquire an audience, because then "No published node 'chem_11_ch1'" is on
    a student's screen.
    """
    assert refuse(429, "Slow down").headers[AUDIENCE_HEADER] == STUDENT


# --- the refusals a student can actually meet ------------------------------------------


def _active(*titles):
    return [{"plan_id": str(i), "scope_title": t, "updated_at": None}
            for i, t in enumerate(titles, start=1)]


def test_the_full_shelf_tells_the_student_which_plan_to_archive():
    """The sentence that used to be replaced by "something changed"."""
    connection = _FakeConnection(
        rows=_active("Gravitation", "Thermodynamics", "Optics"), values=[0]
    )
    with pytest.raises(store.LimitReached) as raised:
        asyncio.run(store.reserve_generation(connection, "student-1", "T", "n"))

    limit = raised.value
    assert limit.status == 409
    assert limit.action == ARCHIVE_PLAN, "the app can offer the way out, not just say it"
    assert "Gravitation" in limit.detail, "the stalest plan is the one to name"
    assert "Archive one first" in limit.detail


def test_a_shelf_full_of_half_built_plans_says_to_wait_rather_than_to_archive():
    """Telling a student to archive nothing would be nonsense.

    `active` is empty and the cap is reached entirely by generations still in flight, so
    the way out is time, not a decision.
    """
    connection = _FakeConnection(rows=[], values=[store.MAX_ACTIVE_PLANS])
    with pytest.raises(store.LimitReached) as raised:
        asyncio.run(store.reserve_generation(connection, "student-1", "T", "n"))

    assert raised.value.status == 409
    assert raised.value.action == "wait"
    assert "being built" in raised.value.detail


@pytest.mark.parametrize(
    "hour, day, expected",
    [
        (store.MAX_PLANS_PER_HOUR, 0, "Try again a bit later."),
        (0, store.MAX_PLANS_PER_DAY, "Try again tomorrow."),
    ],
)
def test_the_spending_limits_say_how_long_rather_than_just_too_many(hour, day, expected):
    """A day-long wait told as "give it a minute" is a student refreshing for an hour.

    These are the app's generic 429 line, which says a minute, against the server's,
    which knows which of the two limits was hit.
    """
    connection = _FakeConnection(rows=[], row={"hour": hour, "day": day}, values=[0])
    with pytest.raises(store.LimitReached) as raised:
        asyncio.run(store.reserve_generation(connection, "student-1", "T", "n"))

    assert raised.value.status == 429
    assert raised.value.detail.endswith(expected)
    assert raised.value.action == "wait"


def test_every_limit_carries_a_sentence_and_an_action():
    """A refusal with no way out is the bug this all exists for.

    Parameterised over the store's own raises rather than a hand-written list, so a
    fifth limit added later has to say what to do about it too.
    """
    cases = [
        (_active("A", "B", "C"), {"hour": 0, "day": 0}, [0]),
        ([], {"hour": 0, "day": 0}, [store.MAX_ACTIVE_PLANS]),
        ([], {"hour": store.MAX_PLANS_PER_HOUR, "day": 0}, [0]),
        ([], {"hour": 0, "day": store.MAX_PLANS_PER_DAY}, [0]),
    ]
    for rows, row, values in cases:
        connection = _FakeConnection(rows=rows, row=row, values=values)
        with pytest.raises(store.LimitReached) as raised:
            asyncio.run(store.reserve_generation(connection, "student-1", "T", "n"))
        assert raised.value.detail.strip(), "a refusal with no sentence"
        assert raised.value.action in {"wait", ARCHIVE_PLAN}, "a refusal with no way out"
        # The line the app would have shown instead, for every one of them.
        assert raised.value.detail != "Something changed while you were here. Try again."


# --- the routes that speak to the student ----------------------------------------------


def test_the_refusals_written_in_jeenes_voice_are_sent_as_the_students():
    """Ask Jeene answers in the first person, and the app's generic lines erase that.

    "I could not reach my notes just now" becoming "The server had a problem" is the
    same defect as the plan cap: the sentence that was written for the student exists,
    and is replaced by one that says less. Source-inspected, in the style this repo uses
    for rules that live in a handler rather than in a return value.
    """
    import inspect

    from app.routers import doubts as doubts_router

    source = inspect.getsource(doubts_router.ask)
    assert "refuse(" in source
    assert "raise HTTPException(\n            status_code=422" not in source
    assert "raise HTTPException(\n            status_code=503" not in source


def test_the_planner_read_limits_say_which_wait_it_is():
    """Two 429s, minutes apart in meaning: a few minutes, or tomorrow."""
    import inspect

    source = inspect.getsource(plans_module._rate_limit_reads)
    assert source.count("refuse(") == 2
    assert "try again in a few minutes" in source
    assert "Try again tomorrow." in source
