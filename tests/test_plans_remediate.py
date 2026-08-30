"""What a missed checkpoint does to the plan.

The checkpoint is the claim the feature rests on. A miss that changed nothing would leave
the student facing the same questions whose answers they had just read — passing that
proves only that they can remember eight answers for five minutes. So a miss grows the
plan around what the check found, and clears the check so the retake is unseen.

These are integration tests because both halves are writes across three tables, and the
one thing worth proving is that the second attempt is genuinely a second attempt.
"""

import asyncio
import os
import uuid

import asyncpg
import pytest
from fastapi.testclient import TestClient

from app.plans import remediate

integration = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; these need a real database",
)

SCOPE = "phy_11_ch8_s1"
STUDENT = "student-remediation"


@pytest.fixture(scope="module")
def client():
    from app.auth import current_tenant, optional_user, require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: {"uid": STUDENT}
    app.dependency_overrides[optional_user] = lambda: {"uid": STUDENT}
    app.dependency_overrides[current_tenant] = lambda: "JEENE_MASTER"
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean(request):
    """This student exists only for these tests, and starts each one with nothing.

    Created here rather than in the seed so the file stands on its own: these tests write
    attempts, and sharing a student with anything else would make them read each other's.
    """
    if not os.environ.get("DATABASE_URL"):
        return

    async def reset():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "INSERT INTO users (firebase_uid, tenant_id) VALUES ($1, 'JEENE_MASTER') "
                "ON CONFLICT (firebase_uid) DO NOTHING",
                STUDENT,
            )
            await conn.execute(
                "DELETE FROM study_plans WHERE firebase_uid = $1", STUDENT
            )
            # The spend ledger too: a deleted plan does not refund its generation.
            await conn.execute(
                "DELETE FROM plan_generations WHERE firebase_uid = $1", STUDENT
            )
            await conn.execute("DELETE FROM attempts WHERE firebase_uid = $1", STUDENT)
        finally:
            await conn.close()

    asyncio.run(reset())


def _answer(question_ids, correct):
    async def write():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            for qid in question_ids:
                await conn.execute(
                    """
                    INSERT INTO attempts (attempt_id, firebase_uid, tenant_id,
                                          question_id, is_correct, time_spent_ms)
                    VALUES ($1, $2, 'JEENE_MASTER', $3, $4, 30000)
                    """,
                    uuid.uuid4(), STUDENT, qid, correct,
                )
        finally:
            await conn.close()

    asyncio.run(write())


def _plan(client):
    response = client.post(
        "/plans",
        json={"scope_node_id": SCOPE, "proficiency": "intermediate",
              "intent": "first_time"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _checkpoint(plan):
    return next(s for s in plan["steps"] if s["completion_kind"] == "checkpoint")


def _open(client, plan_id, step_id):
    response = client.get(f"/plans/{plan_id}/steps/{step_id}/items")
    assert response.status_code == 200, response.text
    return response.json()


def _fail_the_check(client, plan):
    """Open the checkpoint, get every question wrong, hand it in."""
    checkpoint = _checkpoint(plan)
    opened = _open(client, plan["plan_id"], checkpoint["step_id"])
    ids = [q["question_id"] for q in opened["questions"]]
    assert ids, "the checkpoint froze nothing; the test proves nothing"
    _answer(ids, correct=False)
    result = client.post(f"/plans/{plan['plan_id']}/checkpoint/submit")
    assert result.status_code == 200, result.text
    return ids, result.json()


# --- the miss -------------------------------------------------------------------------


@integration
def test_a_miss_adds_work_on_what_the_check_found(client):
    plan = _plan(client)
    _, result = _fail_the_check(client, plan)

    assert result["passed"] is False
    assert result["plan_status"] == "active", "a miss does not fail the plan"

    added = result["added_steps"]
    assert added, "a miss with nothing added is a locked door"
    assert all(s["remediation_round"] == 1 for s in added)
    for step in added:
        assert step["completion_kind"] == "accuracy"
        assert step["how_to_use"], "a step without guidance is a link"
        assert step["focus_node_ids"], "remediation has to be aimed at something"


@integration
def test_the_added_work_sits_before_the_check_not_after_it(client):
    plan = _plan(client)
    _fail_the_check(client, plan)

    reread = client.get(f"/plans/{plan['plan_id']}").json()
    positions = [s["position"] for s in reread["steps"]]
    assert positions == sorted(positions), "positions must stay a clean order"
    assert positions == list(range(len(positions))), "and have no gaps"

    checkpoint_position = _checkpoint(reread)["position"]
    added = [s for s in reread["steps"] if s["remediation_round"] > 0]
    assert added, "the added steps should be on the plan when it is re-read"
    assert all(s["position"] < checkpoint_position for s in added), (
        "work after the step that decides whether you are finished is work nobody does"
    )


@integration
def test_the_retake_is_not_the_same_questions(client):
    """The whole reason a miss has to do something.

    Answers are revealed as they are given, so a second attempt at the same deck measures
    whether a student can remember eight answers, which is not what the plan claims.
    """
    plan = _plan(client)
    first, _ = _fail_the_check(client, plan)

    checkpoint = _checkpoint(client.get(f"/plans/{plan['plan_id']}").json())
    second = [
        q["question_id"]
        for q in _open(client, plan["plan_id"], checkpoint["step_id"])["questions"]
    ]

    assert second, "the checkpoint refroze to nothing"
    assert not (set(first) & set(second)), (
        f"{len(set(first) & set(second))} of the retake's questions were in the first"
    )


@integration
def test_the_attempts_are_kept(client):
    """The record of what happened is not edited to make the next number look better."""
    plan = _plan(client)
    ids, _ = _fail_the_check(client, plan)

    async def count():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT count(*) FROM attempts WHERE firebase_uid = $1 "
                "AND question_id = ANY($2::text[])",
                STUDENT, ids,
            )
        finally:
            await conn.close()

    assert asyncio.run(count()) == len(ids)


@integration
def test_the_plan_stops_growing(client):
    """Past the cap the check still reopens — a student always gets another honest
    attempt — but the plan does not keep getting longer."""
    plan = _plan(client)

    rounds = []
    for _ in range(remediate.MAX_ROUNDS + 2):
        current = client.get(f"/plans/{plan['plan_id']}").json()
        _, result = _fail_the_check(client, current)
        rounds.append(len(result["added_steps"]))

    assert all(n > 0 for n in rounds[: remediate.MAX_ROUNDS]), rounds
    assert all(n == 0 for n in rounds[remediate.MAX_ROUNDS :]), rounds

    # And the last refusal still gave them a fresh check to sit.
    final = client.get(f"/plans/{plan['plan_id']}").json()
    assert _open(client, plan["plan_id"], _checkpoint(final)["step_id"])["questions"]


# --- passing --------------------------------------------------------------------------


@integration
def test_passing_adds_nothing(client):
    plan = _plan(client)
    checkpoint = _checkpoint(plan)
    opened = _open(client, plan["plan_id"], checkpoint["step_id"])
    ids = [q["question_id"] for q in opened["questions"]]
    _answer(ids, correct=True)

    result = client.post(f"/plans/{plan['plan_id']}/checkpoint/submit").json()

    assert result["passed"] is True
    assert result["added_steps"] == []
    # And the checkpoint keeps its questions: it was passed on those, and clearing them
    # would throw away what the pass was based on.
    reread = _checkpoint(client.get(f"/plans/{plan['plan_id']}").json())
    assert reread["items"][0]["question_count"] == len(ids)


@integration
def test_the_same_concept_is_not_added_twice(client):
    """Two rounds that both catch the same idea should leave one step for it.

    A rail with "Fix: variation with depth" on it twice reads as a bug, and the second
    copy is not work the student did not already have.
    """
    plan = _plan(client)
    _fail_the_check(client, plan)
    _fail_the_check(client, client.get(f"/plans/{plan['plan_id']}").json())

    steps = client.get(f"/plans/{plan['plan_id']}").json()["steps"]
    added = [s for s in steps if s["remediation_round"] > 0]
    assert len(added) > 1, "the test needs both rounds to have added something"

    concepts = [n for s in added for n in s["focus_node_ids"]]
    assert len(concepts) == len(set(concepts)), concepts
    titles = [s["title"] for s in added]
    assert len(titles) == len(set(titles)), titles


@integration
def test_handing_in_twice_adds_work_only_once(client):
    """Submitting a missed checkpoint again must be a no-op, not a second round.

    This is the sequential half of the guarantee, and it is what this harness can
    honestly test: `TestClient` does not give real parallelism, so a test that fired two
    submits at once passed with the row lock *removed* and proved nothing. The
    concurrent half is asserted below by reading the source, and was verified against a
    real server with two simultaneous requests — before the fix that pair returned one
    200 and one 500.
    """
    plan = _plan(client)
    checkpoint = _checkpoint(plan)
    opened = _open(client, plan["plan_id"], checkpoint["step_id"])
    _answer([q["question_id"] for q in opened["questions"]], correct=False)

    url = f"/plans/{plan['plan_id']}/checkpoint/submit"
    first = client.post(url).json()
    second = client.post(url).json()

    assert len(first["added_steps"]) > 0
    assert second["added_steps"] == [], "the second submit must not remediate again"

    steps = client.get(f"/plans/{plan['plan_id']}").json()["steps"]
    rounds = {s["remediation_round"] for s in steps if s["remediation_round"] > 0}
    assert rounds == {1}, rounds


def test_the_submit_holds_the_plan_row_for_the_whole_decision():
    """The concurrent half, which a synchronous test client cannot exercise.

    Appending steps and clearing the checkpoint were separate writes. Two simultaneous
    submits read the same frozen questions, both remediated, and the second insert died
    on the unique constraint — a 500 for double-tapping a button. Both are now inside one
    transaction that holds the plan's row, so the second waits and then sees a checkpoint
    that has already been cleared.
    """
    import inspect

    from app.plans import store
    from app.routers import plans as plans_router

    assert "FOR UPDATE" in inspect.getsource(store.lock_plan)

    source = inspect.getsource(plans_router.submit_checkpoint)
    assert "async with connection.transaction():" in source
    assert "store.lock_plan" in source
    # The lock must be taken before anything is read, or it guards nothing.
    assert source.index("lock_plan") < source.index("items_for")
    # ...and both writes must be inside it.
    transaction_at = source.index("async with connection.transaction():")
    assert transaction_at < source.index("_remediate(")
    assert transaction_at < source.index("set_plan_status")
