"""Jeene Mode against a real database.

Everything else in this suite runs without one, which is what let four tickets be built
before a single query was executed. These are the checks that only a database can make:
that the SQL is valid, that the arguments encode, that the constraints bite, that a
selector frozen on Monday returns the same questions on Tuesday.

Needs a local Postgres with the schemas and `db/testdata/seed_plans.sql` loaded — see
that file's header. Auth is the one thing stubbed: Firebase tokens cannot be minted here,
so `require_user` is overridden and everything else is the real code path.
"""

import os
import uuid

import pytest
from fastapi.testclient import TestClient

integration = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; these need a real database",
)

SCOPE = "phy_11_ch8_s1"          # Acceleration due to gravity (subtopic)
CHAPTER = "phy_11_ch8"           # Gravitation
OTHER_SCOPE = "phy_11_ch5_s1"    # The second law
STUDENT = "student-with-history"
FRESH = "student-fresh"
SCRATCH = "student-scratch"    # the only student these tests write attempts for

# The seed's shape, named rather than repeated as literals. Three concepts under the
# subtopic, each with three question types x four difficulties x four variants.
CONCEPTS_IN_SCOPE = 3
PER_CONCEPT = 3 * 4 * 4
SCOPE_TOTAL = CONCEPTS_IN_SCOPE * PER_CONCEPT
PER_DIFFICULTY = 3 * 4


@pytest.fixture(scope="module")
def client():
    from app.auth import current_tenant, optional_user, require_admin, require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: {"uid": STUDENT}
    app.dependency_overrides[optional_user] = lambda: {"uid": STUDENT}
    app.dependency_overrides[current_tenant] = lambda: "JEENE_MASTER"
    app.dependency_overrides[require_admin] = lambda: {
        "uid": STUDENT, "tenant_id": "JEENE_MASTER", "admin_email": "a@b.c"
    }
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clean_plans():
    """Every test starts with no plans. Plans are the only thing these tests write."""
    import asyncio

    import asyncpg

    async def wipe():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        await conn.execute("DELETE FROM study_plans")
        # And the spend ledger. Deleting a plan does not refund its generation — that is
        # deliberate, and it is why a suite that only cleared plans started tripping the
        # hourly limit partway through.
        await conn.execute("DELETE FROM plan_generations")
        # The seed's attempts are all backdated; anything at `now()` was written by a
        # test. Without this, one test's answers silently become the next test's record —
        # which is exactly what happened, and it moved a foundation step.
        await conn.execute(
            "DELETE FROM attempts WHERE created_at > now() - interval '1 minute'"
        )
        await conn.close()

    if os.environ.get("DATABASE_URL"):
        asyncio.run(wipe())
    yield


def _as(client, uid):
    """Run the next requests as a different student."""
    from app.auth import optional_user, require_user

    client.app.dependency_overrides[require_user] = lambda: {"uid": uid}
    client.app.dependency_overrides[optional_user] = lambda: {"uid": uid}


def _open(client, plan_id, step_id):
    """Open a step the way the app does, which is what freezes its questions."""
    response = client.get(f"/plans/{plan_id}/steps/{step_id}/items")
    assert response.status_code == 200, response.text
    return response.json()


def _first_question_step(plan):
    return next(
        s for s in plan["steps"]
        if any(i["type"] == "questions" for i in s["items"])
    )


def _create(client, scope=SCOPE, **kw):
    body = {"scope_node_id": scope, "proficiency": "intermediate",
            "intent": "first_time"}
    body.update(kw)
    return client.post("/plans", json=body)


# --- the boundary, against real content -------------------------------------------------


@integration
def test_the_inventory_carries_no_content_from_a_real_question_bank(client):
    """The guard that has only ever been checked against source text, now against data.

    Every seeded question's stem, options, key and worked solution are distinctive
    strings. None of them may appear anywhere in what a planner would be sent.
    """
    response = client.get("/plans/debug/inventory", params={"node_id": SCOPE})
    assert response.status_code == 200
    body = response.text

    for leaked in (
        "STEM ", "OPTION-A", "OPTION-B", "WORKED-SOLUTION-TEXT",
        "AI-EXPLANATION-TEXT", "SOLUTION-FIGURE", "STEM-FIGURE", "NOTES-PDF-URL",
    ):
        assert leaked not in body, f"{leaked!r} reached the planner"

    payload = response.json()
    # And no question ids: the planner picks a filter, never a question.
    assert "c_height_mcq_easy" not in body
    assert payload["scope"]["title"] == "Acceleration due to gravity"
    assert payload["question_buckets"], "the catalogue should not be empty"


@integration
def test_bucket_totals_really_do_over_count_a_multi_tagged_question(client):
    """`scope_question_total` exists because of exactly this, and now it is measured.

    One seeded question is tagged to two concepts in the scope, so the buckets add up to
    one more than the scope actually holds.
    """
    payload = client.get(
        "/plans/debug/inventory", params={"node_id": SCOPE}
    ).json()
    bucket_sum = sum(b["total"] for b in payload["question_buckets"])
    assert bucket_sum == payload["scope_question_total"] + 1


@integration
def test_a_question_from_an_unreleased_paper_is_in_no_count_and_no_deck(client):
    payload = client.get("/plans/debug/inventory", params={"node_id": SCOPE}).json()
    # The seeded questions, and not the one belonging to the unreleased paper.
    assert payload["scope_question_total"] == SCOPE_TOTAL

    questions = client.get(
        "/concepts/c_height/questions", params={"limit": 100}
    ).json()
    ids = [q["question_id"] for q in questions["items"]]
    assert "c_height_paper_q1" not in ids


@integration
def test_the_authored_cross_chapter_prerequisite_is_found(client):
    """The exact case the feature was asked for: force before gravitation."""
    payload = client.get("/plans/debug/inventory", params={"node_id": SCOPE}).json()
    authored = [
        c for c in payload["foundation_candidates"] if c["source"] == "authored"
    ]
    assert any(c["node_id"] == "c_newton2" for c in authored)
    found = next(c for c in authored if c["node_id"] == "c_newton2")
    assert found["chapter_title"] == "Laws of Motion"
    assert found["question_count"] >= 3


# --- creating and resuming ----------------------------------------------------------------


@integration
def test_a_plan_is_created_with_real_steps_and_ends_in_a_checkpoint(client):
    _as(client, STUDENT)
    response = _create(client)
    assert response.status_code == 201, response.text
    plan = response.json()
    assert 3 <= len(plan["steps"]) <= 6
    assert plan["steps"][-1]["kind"] == "verify"
    assert plan["steps"][-1]["completion_kind"] == "checkpoint"
    assert plan["scope_title"] == "Acceleration due to gravity"
    assert plan["subject"] == "phy"
    assert plan["summary"]
    for step in plan["steps"]:
        assert 2 <= len(step["how_to_use"]) <= 4
        assert step["why"]


@integration
def test_asking_twice_resumes_rather_than_forking(client):
    _as(client, STUDENT)
    first = _create(client).json()
    second = _create(client)
    assert second.status_code == 201
    assert second.json()["plan_id"] == first["plan_id"]
    assert len(client.get("/plans").json()) == 1


@integration
def test_a_students_record_puts_the_foundation_step_first(client):
    """Six attempts on the prerequisite, two right — evidence of a real gap."""
    _as(client, STUDENT)
    plan = _create(client).json()
    assert plan["steps"][0]["is_foundation"] is True
    assert plan["steps"][0]["focus_node_ids"] == ["c_newton2"]
    assert "Laws of Motion" in plan["steps"][0]["why"]


@integration
def test_a_student_with_no_record_gets_no_invented_weakness(client):
    _as(client, FRESH)
    plan = _create(client).json()
    assert not any(s["is_foundation"] for s in plan["steps"])
    _as(client, STUDENT)


@integration
def test_a_fourth_active_plan_is_refused_and_names_one_to_archive(client):
    _as(client, STUDENT)
    assert _create(client, scope=SCOPE).status_code == 201
    assert _create(client, scope=OTHER_SCOPE).status_code == 201
    assert _create(client, scope=CHAPTER).status_code == 201
    fourth = _create(client, scope="phy_11_ch5")
    assert fourth.status_code == 409
    assert "Archive one first" in fourth.json()["detail"]


@integration
def test_archiving_frees_a_slot(client):
    _as(client, STUDENT)
    first = _create(client, scope=SCOPE).json()
    _create(client, scope=OTHER_SCOPE)
    _create(client, scope=CHAPTER)
    assert client.post(f"/plans/{first['plan_id']}/archive").status_code == 200
    assert _create(client, scope="phy_11_ch5").status_code == 201


@integration
def test_a_scope_with_nothing_published_is_refused_rather_than_stored(client):
    _as(client, STUDENT)
    response = _create(client, scope="phy_11_ch5_t1")
    # That topic does have questions, so this must succeed — the guard is checked by
    # pointing at something unplannable instead.
    assert response.status_code == 201
    assert _create(client, scope="c_height").status_code == 404


# --- freezing, for real -------------------------------------------------------------------


@integration
def test_a_frozen_step_returns_the_same_questions_every_time(client):
    """The whole reason freezing exists, and the one thing no fake connection can prove."""
    _as(client, STUDENT)
    plan = _create(client).json()
    step = _first_question_step(plan)
    opened = _open(client, plan["plan_id"], step["step_id"])
    first_ids = [q["question_id"] for q in opened["questions"]]
    assert first_ids, "a question step must open on questions"

    # Again, twice: once through the same endpoint and once through the plan.
    assert [q["question_id"] for q in
            _open(client, plan["plan_id"], step["step_id"])["questions"]] == first_ids
    again = client.get(f"/plans/{plan['plan_id']}").json()
    step_again = next(s for s in again["steps"] if s["step_id"] == step["step_id"])
    frozen = [q for i in step_again["items"] for q in i["question_ids"]]
    assert frozen == first_ids


@integration
def test_a_question_count_is_real_once_frozen_and_absent_before(client):
    _as(client, STUDENT)
    plan = _create(client).json()

    # The step the student is on is frozen when the plan opens, so its count is real.
    current = next(s for s in plan["steps"] if s["state"] in ("pending", "in_progress"))
    if any(i["type"] == "questions" for i in current["items"]):
        item = next(i for i in current["items"] if i["type"] == "questions")
        assert item["question_count"] == len(item["question_ids"]) > 0

    # A step further down is not, and must say so rather than estimating.
    later = next(
        s for s in plan["steps"]
        if s["position"] > current["position"]
        and any(i["type"] == "questions" for i in s["items"])
    )
    before = next(i for i in later["items"] if i["type"] == "questions")
    assert before["planned_count"] > 0
    assert before["question_count"] is None, "unopened steps must not guess"
    assert before["question_ids"] == []

    _open(client, plan["plan_id"], later["step_id"])
    after_plan = client.get(f"/plans/{plan['plan_id']}").json()
    after = next(
        i for s in after_plan["steps"] if s["step_id"] == later["step_id"]
        for i in s["items"] if i["type"] == "questions"
    )
    assert after["question_count"] == len(after["question_ids"]) > 0


@integration
def test_the_checkpoint_is_not_frozen_before_the_student_reaches_it(client):
    """Freezing it early would fill it with the questions they are about to meet."""
    _as(client, STUDENT)
    plan = _create(client).json()
    checkpoint = plan["steps"][-1]
    assert checkpoint["completion_kind"] == "checkpoint"
    assert all(i["question_count"] is None for i in checkpoint["items"])


@integration
def test_opening_a_step_returns_its_questions_without_any_answer(client):
    _as(client, STUDENT)
    plan = _create(client).json()
    step = next(s for s in plan["steps"] if any(
        i["type"] == "questions" for i in s["items"]))
    response = client.get(f"/plans/{plan['plan_id']}/steps/{step['step_id']}/items")
    assert response.status_code == 200
    body = response.text
    assert "WORKED-SOLUTION-TEXT" not in body
    assert "SOLUTION-FIGURE" not in body
    payload = response.json()
    assert payload["questions"], "a question step must open on questions"
    for question in payload["questions"]:
        assert "correct_option_ids" not in question
        assert question["question_text"].startswith("STEM ")
        for figure in question["figures"]:
            assert figure["placement"] in ("stem", "option")


# --- the refusals -----------------------------------------------------------------------------


@integration
def test_a_graded_step_cannot_be_tapped_done_but_a_learn_step_can(client):
    _as(client, STUDENT)
    plan = _create(client).json()
    graded = next(s for s in plan["steps"] if s["completion_kind"] != "self")
    refused = client.post(
        f"/plans/{plan['plan_id']}/steps/{graded['step_id']}/complete"
    )
    assert refused.status_code == 422

    learn = next((s for s in plan["steps"] if s["completion_kind"] == "self"), None)
    if learn is not None:
        ok = client.post(f"/plans/{plan['plan_id']}/steps/{learn['step_id']}/complete")
        assert ok.status_code == 200
        updated = next(
            s for s in ok.json()["steps"] if s["step_id"] == learn["step_id"]
        )
        assert updated["state"] == "done"


@integration
def test_the_checkpoint_cannot_be_skipped_but_other_steps_can(client):
    _as(client, STUDENT)
    plan = _create(client).json()
    checkpoint = plan["steps"][-1]
    assert client.post(
        f"/plans/{plan['plan_id']}/steps/{checkpoint['step_id']}/skip"
    ).status_code == 422

    other = plan["steps"][1]
    skipped = client.post(f"/plans/{plan['plan_id']}/steps/{other['step_id']}/skip")
    assert skipped.status_code == 200
    updated = next(s for s in skipped.json()["steps"] if s["step_id"] == other["step_id"])
    assert updated["state"] == "skipped"


@integration
def test_another_students_plan_is_not_found(client):
    _as(client, STUDENT)
    plan = _create(client).json()
    _as(client, FRESH)
    assert client.get(f"/plans/{plan['plan_id']}").status_code == 404
    assert client.post(f"/plans/{plan['plan_id']}/archive").status_code == 404
    _as(client, STUDENT)


@integration
def test_the_batch_endpoint_returns_only_what_your_own_plans_froze(client):
    _as(client, STUDENT)
    plan = _create(client).json()
    step = _first_question_step(plan)
    mine = _open(client, plan["plan_id"], step["step_id"])["questions"][0]["question_id"]
    response = client.get(
        "/questions", params={"ids": [mine, "c_latitude_pyq_hard", "does_not_exist"]}
    )
    assert response.status_code == 200
    returned = [q["question_id"] for q in response.json()]
    assert returned == [mine], "only the id this student's plan actually froze"


# --- progress, from real attempts ---------------------------------------------------------------


@integration
def test_answering_a_steps_questions_moves_it_without_anyone_marking_it(client):
    """Completion is evidence. This is that claim, executed."""
    import asyncio

    import asyncpg

    _as(client, SCRATCH)
    plan = _create(client).json()
    step = next(
        s for s in plan["steps"]
        if s["completion_kind"] == "accuracy"
        and any(i["type"] == "questions" for i in s["items"])
    )
    ids = [q["question_id"] for q in
           _open(client, plan["plan_id"], step["step_id"])["questions"]]
    assert ids
    required = step["required_questions"]

    async def answer(question_ids, correct):
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        for qid in question_ids:
            await conn.execute(
                """
                INSERT INTO attempts (attempt_id, firebase_uid, tenant_id, question_id,
                                      is_correct, time_spent_ms)
                VALUES ($1, $2, 'JEENE_MASTER', $3, $4, 30000)
                """,
                uuid.uuid4(), SCRATCH, qid, correct,
            )
        await conn.close()

    # Answer everything wrong first: enough answered, not enough right.
    asyncio.run(answer(ids, False))
    after_wrong = client.get(f"/plans/{plan['plan_id']}").json()
    state = next(s for s in after_wrong["steps"] if s["step_id"] == step["step_id"])
    assert state["state"] == "in_progress"
    assert state["answered"] == len(ids)
    assert state["correct"] == 0

    # Then answer them right. Latest attempt wins, so the step completes.
    asyncio.run(answer(ids, True))
    after_right = client.get(f"/plans/{plan['plan_id']}").json()
    state = next(s for s in after_right["steps"] if s["step_id"] == step["step_id"])
    assert state["correct"] == len(ids), "wrong-then-right must count as right"
    assert state["state"] == "done"
    assert state["answered"] >= required
    _as(client, STUDENT)


@integration
def test_the_history_percentage_matches_the_plan_screen(client):
    """The bug the audit caught, checked against the database that would have shown it."""
    _as(client, STUDENT)
    plan = _create(client).json()
    from_detail = client.get(f"/plans/{plan['plan_id']}").json()["percent"]
    from_history = next(
        p for p in client.get("/plans").json() if p["plan_id"] == plan["plan_id"]
    )["percent"]
    assert from_history == from_detail


# --- the widened endpoint ---------------------------------------------------------------------


@integration
def test_the_concept_question_filters_narrow_without_breaking_the_total(client):
    unfiltered = client.get("/concepts/c_height/questions", params={"limit": 100}).json()
    assert unfiltered["total"] == len(unfiltered["items"]) == PER_CONCEPT

    easy = client.get(
        "/concepts/c_height/questions", params={"difficulty": "easy", "limit": 100}
    ).json()
    assert easy["total"] == len(easy["items"]) == PER_DIFFICULTY
    assert all(q["difficulty"] == "easy" for q in easy["items"])

    # `unrated` is not a value in the column, and once collapsed to "no filter" it
    # matched every question in the concept rather than the ungraded ones.
    unrated = client.get(
        "/concepts/c_height/questions", params={"difficulty": "unrated", "limit": 100}
    ).json()
    assert unrated["total"] == PER_DIFFICULTY
    assert all(q["difficulty"] is None for q in unrated["items"])


@integration
def test_excluding_seen_questions_actually_excludes_them(client):
    _as(client, STUDENT)
    seen = client.get(
        "/concepts/c_depth/questions", params={"exclude_seen": True, "limit": 100}
    ).json()
    everything = client.get("/concepts/c_depth/questions", params={"limit": 100}).json()
    assert seen["total"] < everything["total"], "this student has answered nine of these"
    assert seen["total"] == len(seen["items"])


@integration
def test_an_unknown_difficulty_is_refused(client):
    response = client.get(
        "/concepts/c_height/questions", params={"difficulty": "banana"}
    )
    assert response.status_code == 422


# --- limits, counted where they have to be ------------------------------------------------------


@integration
def test_the_spending_limit_is_counted_in_the_database_not_the_process(client):
    """Rows written by another connection must count against this one's limit.

    That is the whole claim: Render runs several workers and restarts them freely, so a
    limit that lives in a process is a limit that resets when the platform feels like it.
    Inserting the rows out of band is the closest a single-process test gets to a second
    worker, and it fails for the right reason if the counter is ever moved in-process.
    """
    import asyncio

    import asyncpg

    from app.plans import store

    async def insert_generations(n):
        """The spend is counted over `plan_generations`, not over plans.

        A generation that failed still cost money, and a plan that was deleted does not
        refund one — so the ledger is what the limit reads, and it is what a second
        worker would have written.
        """
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        for _ in range(n):
            await conn.execute(
                """
                INSERT INTO plan_generations (generation_id, firebase_uid, tenant_id,
                                              scope_node_id, outcome)
                VALUES ($1, $2, 'JEENE_MASTER', $3, 'saved')
                """,
                uuid.uuid4(), STUDENT, SCOPE,
            )
        await conn.close()

    _as(client, STUDENT)
    asyncio.run(insert_generations(store.MAX_PLANS_PER_HOUR))
    refused = _create(client)
    assert refused.status_code == 429, refused.text
    assert "hour" in refused.json()["detail"].lower()


# --- placement ---------------------------------------------------------------------------------


@integration
def test_the_placement_check_returns_answerless_questions(client):
    _as(client, FRESH)
    response = client.get("/plans/placement", params={"node_id": SCOPE})
    assert response.status_code == 200
    questions = response.json()
    assert 0 < len(questions) <= 5
    assert "WORKED-SOLUTION-TEXT" not in response.text
    for question in questions:
        assert "correct_option_ids" not in question
    _as(client, STUDENT)


@integration
def test_the_hourly_limit_leaves_room_to_replace_a_plan(client):
    """Set equal to the active cap, archiving to make room left you rate-limited.

    Two limits that individually look reasonable and together make the feature unusable —
    which is only visible by running the sequence a student would.
    """
    from app.plans import store

    assert store.MAX_PLANS_PER_HOUR > store.MAX_ACTIVE_PLANS
    assert store.MAX_PLANS_PER_DAY >= store.MAX_PLANS_PER_HOUR


@integration
def test_a_step_never_asks_for_more_questions_than_it_has(client):
    """`required_questions` is set against what the planner asked for; freezing finds what
    exists, and it can be fewer.

    That is not a labelling problem. Completion is `answered >= required_questions`, so a
    step asking for nine with eight frozen can never be done and the plan containing it
    can never complete — while the card cheerfully tells the student to answer nine of
    eight. Seen live on a real plan: planned 9, froze 8.

    The bar is forced here rather than waited for. Whether a given generated plan happens
    to overshoot is up to the model and the catalogue, and a test that only fails when it
    does is a test that passes for the wrong reason — this one was written that way first
    and went green with the fix removed.
    """
    _as(client, FRESH)
    plan = client.post(
        "/plans",
        json={"scope_node_id": SCOPE, "proficiency": "basic", "intent": "first_time"},
    ).json()

    graded = next(
        s for s in plan["steps"]
        if s["completion_kind"] != "self" and s["required_questions"] is not None
    )
    import asyncio

    import asyncpg

    async def force_impossible_bar():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            await conn.execute(
                "UPDATE study_plan_steps SET required_questions = 999 "
                "WHERE step_id = $1::uuid",
                graded["step_id"],
            )
        finally:
            await conn.close()

    asyncio.run(force_impossible_bar())

    opened = client.get(
        f"/plans/{plan['plan_id']}/steps/{graded['step_id']}/items"
    ).json()
    frozen = sum(
        item["question_count"] or 0
        for item in opened["items"]
        if item["question_count"] is not None
    )
    assert frozen > 0, "nothing froze; the test proved nothing"

    reread = next(
        s for s in client.get(f"/plans/{plan['plan_id']}").json()["steps"]
        if s["step_id"] == graded["step_id"]
    )
    assert reread["required_questions"] == frozen, (
        f"asks for {reread['required_questions']} of {frozen} questions, "
        "which can never be answered"
    )
    _as(client, STUDENT)


@integration
def test_generating_a_plan_does_not_hold_a_pooled_connection(client, monkeypatch):
    """The pool has five connections and generation can take ninety seconds.

    Held across that wait, five students planning at once made the whole API unavailable
    to everybody else — browsing, attempts, sign-in. So this asserts the thing directly:
    while generation is running, the connection this request used is back in the pool.
    """
    from app.db import get_pool
    from app.routers import plans as plans_router

    seen: dict = {}
    original = plans_router._generate

    async def watched(inventory):
        pool = get_pool()
        # Idle == size means nothing is checked out — including by us.
        seen["idle"] = pool.get_idle_size()
        seen["size"] = pool.get_size()
        return await original(inventory)

    monkeypatch.setattr(plans_router, "_generate", watched)

    _as(client, FRESH)
    assert _create(client).status_code == 201
    _as(client, STUDENT)

    assert seen, "generation never ran"
    assert seen["idle"] == seen["size"], (
        f"{seen['size'] - seen['idle']} connection(s) held during generation; "
        "the wait must not occupy the pool"
    )


# --- finding a scope from what a student typed ---------------------------------------
#
# The ranking is the half of JM-12 that only a database can check: the scoring is a SQL
# CASE over a normalised title, and whether "Laws of Motion" beats "The second law" is
# decided by rows, not by Python.


def _scopes(client, q, **params):
    response = client.get("/plans/scopes", params={"q": q, **params})
    assert response.status_code == 200, response.text
    return response.json()


@integration
def test_a_chapter_named_exactly_comes_back_first_and_marked_exact(client):
    [top, *_] = _scopes(client, "Gravitation")
    assert top["node_id"] == CHAPTER
    assert top["exact"] is True
    # Everything the app needs to start an intake without another round trip.
    assert top["chapter_node_id"] == CHAPTER
    assert top["subject_name"] == "Physics"
    assert top["question_count"] > 0


@integration
def test_a_whole_sentence_finds_the_chapter_inside_it(client):
    # The cleaned form is what matches here: "i want to study gravitation" is not a
    # title, but with the asking-words removed it is.
    [top, *_] = _scopes(client, "i want to study gravitation")
    assert top["node_id"] == CHAPTER
    assert top["exact"] is True


@integration
def test_a_title_made_of_ordinary_english_still_matches_exactly(client):
    # "of" is a stop-word, so only the raw form can match this title. This is the case
    # that justifies keeping both.
    [top, *_] = _scopes(client, "laws of motion")
    assert top["title"] == "Laws of Motion"
    assert top["exact"] is True


@integration
def test_a_topic_can_be_named_directly(client):
    [top, *_] = _scopes(client, "gravitational field")
    assert top["node_id"] == "phy_11_ch8_t1"
    assert top["type"] == "topic"
    # A topic still reports the chapter above it — beginIntake reads the record by it.
    assert top["chapter_node_id"] == CHAPTER


@integration
def test_a_keyword_finds_a_chapter_whose_title_does_not_contain_the_word(client):
    # 'newton' is in Laws of Motion's search_keywords, not in its title.
    titles = {m["title"] for m in _scopes(client, "newton")}
    assert "Laws of Motion" in titles


@integration
def test_nothing_in_the_syllabus_is_an_empty_list_not_an_error(client):
    assert _scopes(client, "quidditch") == []


@integration
def test_asking_words_alone_find_nothing(client):
    assert _scopes(client, "i want to study the chapter") == []


@integration
def test_a_wildcard_is_matched_literally_and_not_as_a_pattern(client):
    # If '%' ever reached a LIKE pattern this would return the whole syllabus.
    assert _scopes(client, "%") == []
    assert _scopes(client, "gravitation%") != []   # the '%' is simply dropped


@integration
def test_concepts_and_subjects_are_never_offered_as_scopes(client):
    # 'Variation of g with height' is a concept, and a concept is too small to plan.
    for match in _scopes(client, "variation of g with height", limit=10):
        assert match["type"] in {"chapter", "topic", "subtopic"}
    for match in _scopes(client, "physics", limit=10):
        assert match["type"] != "subject"


@integration
def test_the_limit_is_honoured(client):
    assert len(_scopes(client, "gravity", limit=1)) <= 1


@integration
def test_a_found_scope_can_be_planned_without_a_409(client):
    # The whole point of the question-count filter: what search offers, create accepts.
    _as(client, SCRATCH)
    try:
        [top, *_] = _scopes(client, "Gravitation")
        response = client.post(
            "/plans",
            json={
                "scope_node_id": top["node_id"],
                "proficiency": "basic",
                "intent": "first_time",
            },
        )
        assert response.status_code == 201, response.text
    finally:
        _as(client, STUDENT)


# --- reading a message against a real syllabus -------------------------------------------


@integration
def test_the_outline_is_every_plannable_scope_and_nothing_else(client):
    import asyncio
    import asyncpg
    from app.plans import converse

    async def load():
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await converse.load_outline(conn, "JEENE_MASTER")
        finally:
            await conn.close()

    outline = asyncio.run(load())
    ids = {r["node_id"] for r in outline}

    # The seed's two chapters and their topics/subtopics, all of which have questions.
    assert CHAPTER in ids
    assert SCOPE in ids
    assert {r["type"] for r in outline} <= {"chapter", "topic", "subtopic"}
    # Concepts and subjects are not scopes and must never be offered as one.
    assert "c_height" not in ids
    assert "phy" not in ids
    # Everything offered carries what an intake needs.
    for row in outline:
        assert row["question_count"] > 0
        assert row["chapter_node_id"]

    # And it renders to something a model can read without parsing.
    text = converse.outline_text(outline)
    assert f"{CHAPTER} | chapter | Gravitation" in text
    assert "Physics" in text


@integration
def test_a_greeting_never_reaches_the_model(client):
    response = client.post("/plans/interpret", json={"text": "hello"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "greeting"
    assert body["scope"] is None
    assert "study" in body["reply"].lower()


@integration
def test_an_exact_title_is_read_without_the_model(client):
    # The commonest message there is. It must stay free and instant.
    response = client.post("/plans/interpret", json={"text": "Gravitation"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] == "scope"
    assert body["scope"]["node_id"] == CHAPTER


@integration
def test_with_no_model_configured_it_still_offers_what_it_found(client):
    # The default suite runs with the planner disabled, so this is the fallback path:
    # ambiguous words, no model, and the title search's own matches offered instead.
    response = client.post("/plans/interpret", json={"text": "gravity"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["kind"] in {"choose", "unclear"}
    if body["kind"] == "choose":
        assert body["options"], "a choice with nothing to choose from is not a choice"


@integration
def test_an_empty_message_is_answered_rather_than_refused(client):
    response = client.post("/plans/interpret", json={"text": "   "})
    assert response.status_code == 200
    assert response.json()["kind"] == "unclear"


@integration
def test_a_very_long_message_is_refused_by_the_contract(client):
    # An unbounded body is an unbounded prompt, and the prompt is what costs.
    response = client.post("/plans/interpret", json={"text": "x" * 5000})
    assert response.status_code == 422
