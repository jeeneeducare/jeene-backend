"""The plans API: the rules that are refusals, and the shape of what comes back.

Most of what these endpoints do is assembly, and assembly is best checked against a real
database. What is worth testing without one is the set of things the API refuses, because
each refusal is a product decision that a future change could quietly drop: a graded step
must not be completable by tapping, the checkpoint must not be skippable, a fourth plan
must not be created, and `GET /questions` must not fetch an id the caller's plans never
froze. Those are asserted here directly against the handlers.
"""

import asyncio
import inspect

import pytest
from fastapi import HTTPException

from app.plans import store
from app.plans.progress import plan_percent
from app.routers import plans as plans_router
from app.schemas import PlanCreate, PlanDetail, PlanStep, PlanStepItem, PlanSummary


class _FakeConnection:
    def __init__(self, rows=None, row=None, values=None):
        self.rows = rows if rows is not None else []
        self.row = row
        self.values = list(values or [])
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.row

    async def fetchval(self, query, *args):
        self.calls.append((query, args))
        return self.values.pop(0) if self.values else None

    async def execute(self, query, *args):
        self.calls.append((query, args))

    def transaction(self):
        """A no-op stand-in. These tests are about *which* statements run and in what
        order; that they run inside a transaction is asserted separately, by reading the
        source, because a fake cannot demonstrate atomicity."""

        class _Noop:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *exc):
                return False

        return _Noop()


USER = {"uid": "student-1"}


def _step_row(**kw):
    base = dict(
        step_id="22222222-2222-2222-2222-222222222222",
        plan_id="11111111-1111-1111-1111-111111111111",
        position=0,
        kind="practise",
        title="Find your feet",
        why="because",
        how_to_use=["do the first three"],
        focus_node_ids=["c1"],
        is_foundation=False,
        depends_on=[],
        estimated_minutes=16,
        completion_kind="accuracy",
        required_questions=6,
        required_accuracy=0.6,
        state="pending",
        completed_at=None,
    )
    base.update(kw)
    return base


# --- the refusals ---------------------------------------------------------------------


def test_a_graded_step_cannot_be_completed_by_tapping():
    """The plan's claim is that finishing it means something.

    A graded step that could be marked done would make the whole plan tappable, and the
    checkpoint at the end would be measuring nobody.
    """
    connection = _FakeConnection(row=_step_row(completion_kind="accuracy"))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            plans_router.complete_step("p", "s", None, USER, "JEENE_MASTER", connection)
        )
    assert raised.value.status_code == 422
    assert "6" in raised.value.detail, "the refusal should say what would complete it"


def test_a_checkpoint_step_cannot_be_completed_by_tapping_either():
    connection = _FakeConnection(row=_step_row(completion_kind="checkpoint"))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            plans_router.complete_step("p", "s", None, USER, "JEENE_MASTER", connection)
        )
    assert raised.value.status_code == 422


def test_the_checkpoint_cannot_be_skipped():
    """A plan finished by skipping the only step that measures anything proves nothing."""
    connection = _FakeConnection(row=_step_row(completion_kind="checkpoint"))
    with pytest.raises(HTTPException) as raised:
        asyncio.run(
            plans_router.skip_step("p", "s", None, USER, "JEENE_MASTER", connection)
        )
    assert raised.value.status_code == 422


def test_a_step_of_somebody_elses_plan_is_simply_not_found():
    """Not 403. Telling a caller that a plan exists but is not theirs is still telling."""
    connection = _FakeConnection(row=None)
    with pytest.raises(HTTPException) as raised:
        asyncio.run(plans_router._owned_step(connection, "p", "s", "student-1"))
    assert raised.value.status_code == 404


def test_step_ownership_is_checked_in_the_query_not_afterwards():
    source = inspect.getsource(plans_router._owned_step)
    assert "p.firebase_uid = $3" in source


def test_a_fourth_active_plan_is_refused_and_says_which_to_archive():
    connection = _FakeConnection(
        rows=[
            {"plan_id": "1", "scope_title": "Gravitation", "updated_at": None},
            {"plan_id": "2", "scope_title": "Thermodynamics", "updated_at": None},
            {"plan_id": "3", "scope_title": "Optics", "updated_at": None},
        ],
        values=[0],  # nothing else being generated right now
    )
    with pytest.raises(store.LimitReached) as raised:
        asyncio.run(store.reserve_generation(connection, "student-1", "T", "n"))
    assert raised.value.status == 409
    # The active list is ordered by updated_at, so the first row is the stalest.
    assert "Gravitation" in raised.value.detail


def test_three_active_plans_is_the_cap_that_was_designed_for():
    assert store.MAX_ACTIVE_PLANS == 3


@pytest.mark.parametrize(
    "hour,day,status",
    [
        (0, 0, None),
        (store.MAX_PLANS_PER_HOUR, store.MAX_PLANS_PER_HOUR, 429),
        (0, store.MAX_PLANS_PER_DAY, 429),
        (store.MAX_PLANS_PER_HOUR - 1, store.MAX_PLANS_PER_DAY - 1, None),
    ],
)
def test_the_spending_limits_are_counted_per_account(hour, day, status):
    connection = _FakeConnection(rows=[], row={"hour": hour, "day": day}, values=[0])
    if status is None:
        asyncio.run(store.reserve_generation(connection, "student-1", "T", "n"))
        return
    with pytest.raises(store.LimitReached) as raised:
        asyncio.run(store.reserve_generation(connection, "student-1", "T", "n"))
    assert raised.value.status == status


def test_the_limits_are_counted_in_the_database_not_in_memory():
    """Render runs more than one worker and restarts them freely.

    An in-process counter would be a spending limit that resets whenever the platform
    felt like it.
    """
    source = inspect.getsource(store.reserve_generation)
    assert "FROM plan_generations" in source
    assert "interval '1 hour'" in source and "interval '1 day'" in source
    module = inspect.getsource(store)
    assert "_attempts: dict" not in module, "no in-process counter"


def test_archiving_and_recreating_does_not_refund_the_quota():
    """Create, archive, repeat is the loop a bored student finds first.

    The spend is counted over `plan_generations`, which is append-only and has no status
    to filter on, so deleting or archiving a plan cannot give the money back.
    """
    source = inspect.getsource(store.reserve_generation)
    spend = source[source.index("plan_generations"):]
    assert "status" not in spend, "the spend count must not filter on plan status"


def test_the_decision_and_the_reservation_are_one_transaction():
    """The whole finding, in one assertion.

    Three reads and a much later write is not a limit. Six concurrent requests went
    through a cap of three, and each one was a paid model call.
    """
    source = inspect.getsource(store.reserve_generation)
    assert "async with connection.transaction():" in source
    assert "pg_advisory_xact_lock" in source
    # The lock has to be taken before anything is counted, or it guards nothing.
    assert source.index("pg_advisory_xact_lock") < source.index("MAX_ACTIVE_PLANS")
    assert source.index("pg_advisory_xact_lock") < source.index("INSERT INTO plan_generations")


def test_the_model_call_does_not_hold_a_database_connection():
    """A pooled connection held across generation is a connection nobody else can have.

    The pool has five. Five students planning at once made the API unavailable to
    everyone. So the route takes its own connections for the two short phases and holds
    none across the wait.
    """
    source = inspect.getsource(plans_router.create_plan)
    assert "connection: asyncpg.Connection = Depends(get_connection)" not in source, (
        "a yielded dependency is held until the response is sent"
    )
    assert "Depends(current_tenant)" not in source, "that dependency holds a connection"
    generate_at = source.index("await _generate(inventory)")
    # Every acquire opens and closes before or after the wait, never around it.
    before = source[:generate_at]
    assert before.count("async with pool.acquire()") == 1
    assert source[generate_at:].count("async with pool.acquire()") >= 1


# --- what the batch question endpoint will and will not fetch --------------------------


def test_the_batch_endpoint_only_returns_ids_the_callers_own_plans_froze():
    """An endpoint that returns whatever ids it is handed is a way to read the bank."""
    source = inspect.getsource(
        __import__("app.routers.content", fromlist=["x"]).get_questions_by_ids
    )
    assert "p.firebase_uid = $1" in source
    assert "resolved_question_ids" in source
    assert "q for q in ids if q in permitted" in source


def test_the_batch_endpoint_is_bounded():
    from app.routers.content import _MAX_QUESTION_IDS

    assert 0 < _MAX_QUESTION_IDS <= 200


def test_the_batch_fetch_applies_the_unreleased_paper_guard_underneath():
    """Two locks. The first only holds while every plan is written by us."""
    from app.questions import _BY_IDS_SQL

    assert "released_at IS NOT NULL" in _BY_IDS_SQL
    assert "correct_option_ids" not in _BY_IDS_SQL
    assert "explanation_json" not in _BY_IDS_SQL


def test_the_batch_fetch_returns_questions_in_the_order_asked_for():
    """A step's frozen list is an order — easiest first, or a deliberate spread."""
    source = inspect.getsource(
        __import__("app.questions", fromlist=["x"]).fetch_questions_by_ids
    )
    assert "for qid in question_ids if qid in by_id" in source


# --- the shape that reaches the app ----------------------------------------------------


def test_a_question_count_is_never_guessed_from_what_the_plan_asked_for():
    """A step that says ten and opens on six is a bug the student can see."""
    item = plans_router._item(
        {
            "item_id": "33333333-3333-3333-3333-333333333333",
            "item_type": "questions",
            "ref_id": None,
            "sel_count": 10,
            "resolved_question_ids": None,
            "resolved_at": None,
        },
        _FakeRequest(),
    )
    assert item.planned_count == 10
    assert item.question_count is None, "unknown until frozen, never estimated"


def test_a_frozen_item_reports_what_it_actually_holds():
    item = plans_router._item(
        {
            "item_id": "33333333-3333-3333-3333-333333333333",
            "item_type": "questions",
            "ref_id": None,
            "sel_count": 10,
            "resolved_question_ids": ["q1", "q2", "q3"],
            "resolved_at": "2026-08-29",
        },
        _FakeRequest(),
    )
    assert item.planned_count == 10
    assert item.question_count == 3


class _FakeRequest:
    base_url = "https://api.example.com/"


def test_a_video_item_carries_a_player_url_on_our_own_domain():
    """YouTube's embed checks the origin and the referrer, which is why /player exists."""
    item = plans_router._item(
        {
            "item_id": "44444444-4444-4444-4444-444444444444",
            "item_type": "video",
            "ref_id": "abc123XYZ01",
            "sel_count": None,
            "resolved_question_ids": None,
            "resolved_at": None,
        },
        _FakeRequest(),
    )
    assert item.player_url == "https://api.example.com/player?v=abc123XYZ01"
    assert item.video_id == "abc123XYZ01"
    assert item.notes_chapter_id is None


def test_history_and_the_plan_screen_derive_state_the_same_way():
    """A plan that reads 60% on one screen and 40% on the next is worse than either.

    Structural rather than textual: both handlers must reach state through the one
    derivation, so neither can grow its own idea of when a step is done.
    """
    assert "state_from_counts" in inspect.getsource(plans_router._derived_states)
    for handler in (plans_router.list_plans, plans_router.get_plan,
                    plans_router.archive_plan, plans_router.submit_checkpoint):
        assert "_derived_states" in inspect.getsource(handler), handler.__name__


def test_the_history_percentage_is_derived_rather_than_read_off_the_column():
    rows = [
        {
            "plan_id": "p1", "step_id": "s1", "position": 0, "state": "pending",
            "completion_kind": "accuracy", "required_questions": 3,
            "required_accuracy": 0.6, "answered": 4, "correct": 4, "offered": 4,
        },
        {
            "plan_id": "p1", "step_id": "s2", "position": 1, "state": "pending",
            "completion_kind": "checkpoint", "required_questions": 8,
            "required_accuracy": 0.7, "answered": 0, "correct": 0, "offered": 10,
        },
    ]
    connection = _FakeConnection(rows=rows)
    by_plan, by_step = asyncio.run(
        plans_router._derived_states(connection, ["p1"], "student-1", "JEENE_MASTER")
    )
    # The column says pending for both; the log says the first one is done.
    assert by_plan["p1"] == ["done", "pending"]
    assert by_step["s1"][0] == "done"
    assert plan_percent(by_plan["p1"]) == 0.5


# --- generation seam --------------------------------------------------------------------


def test_the_planner_seam_reports_which_planner_produced_the_plan():
    """`origin` is what makes 'why did this student get a thin plan' answerable."""
    from app.plans.schema import (
        Inventory,
        PlanConstraints,
        QuestionBucket,
        ScopeInfo,
        TeachingNode,
    )

    inventory = Inventory(
        scope=ScopeInfo(node_id="n", type="subtopic", title="A topic"),
        concepts=[TeachingNode(node_id="c1", title="c1")],
        question_buckets=[
            QuestionBucket(node_id="c1", question_type="mcq",
                           difficulty="medium", total=20)
        ],
        scope_question_total=20,
        constraints=PlanConstraints(available_question_types=["mcq"]),
    )
    plan, origin, provider, model = asyncio.run(plans_router._generate(inventory))
    assert origin == "fallback"
    assert provider is None and model is None
    assert plan.steps, "the deterministic planner must produce a real plan"


def test_adding_a_provider_is_a_change_to_one_function():
    """The fallback must stay the thing that runs, not become an unexercised error path."""
    assert inspect.iscoroutinefunction(plans_router._generate)
    source = inspect.getsource(plans_router.create_plan)
    assert "_generate(inventory)" in source
    assert "fallback.plan" not in source, "the router must not know which planner ran"


def test_a_scope_with_nothing_published_is_refused_rather_than_stored():
    """A plan row with no steps sits in the history looking like work the student failed."""
    source = inspect.getsource(plans_router.create_plan)
    assert "if not plan.steps:" in source
    assert "status_code=409" in source


def test_resuming_a_plan_costs_neither_a_slot_nor_a_generation():
    source = inspect.getsource(plans_router.create_plan)
    existing_at = source.index("active_plan_for_scope")
    reserve_at = source.index("reserve_generation")
    assert existing_at < reserve_at, "the resume check must come first"


# --- contract ---------------------------------------------------------------------------


def test_the_plan_detail_is_a_summary_plus_its_steps():
    assert issubclass(PlanDetail, PlanSummary)
    assert "steps" in PlanDetail.model_fields


def test_the_wire_format_carries_no_answer():
    for model in (PlanSummary, PlanDetail, PlanStep, PlanStepItem):
        fields = set(model.model_fields)
        for banned in ("answer", "correct_option_ids", "explanation", "solution"):
            assert banned not in fields, f"{model.__name__}.{banned}"


def test_creating_a_plan_takes_only_what_the_student_was_asked():
    """Two chip questions, and the placement score if they took one. Nothing else."""
    assert set(PlanCreate.model_fields) == {
        "scope_node_id",
        "proficiency",
        "intent",
        "placement_correct",
        "placement_of",
    }


def test_every_plan_endpoint_requires_a_signed_in_student():
    """Everything here needs the attempt log and somewhere to persist."""
    for name in (
        "list_plans", "get_plan", "create_plan", "step_items",
        "complete_step", "skip_step", "submit_checkpoint", "archive_plan",
        "placement_check",
    ):
        handler = getattr(plans_router, name)
        signature = inspect.signature(handler)
        assert "user" in signature.parameters, name
        assert "require_user" in str(signature.parameters["user"].default), name


def test_every_plan_endpoint_declares_a_typed_response():
    """The OpenAPI docs are the app team's contract, so `list` is not a response model."""
    from app.main import app

    spec = app.openapi()
    for path, methods in spec["paths"].items():
        if not path.startswith("/plans"):
            continue
        for method, operation in methods.items():
            schema = (
                operation["responses"]
                .get("200", operation["responses"].get("201", {}))
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
            )
            assert schema, f"{method.upper()} {path} has no response schema"
            named = "$ref" in schema or "$ref" in schema.get("items", {})
            assert named, f"{method.upper()} {path} returns an untyped body"


def test_losing_the_create_race_resumes_rather_than_five_hundreds():
    """Two taps, two workers, one scope. The unique index is what makes this idempotent."""
    source = inspect.getsource(plans_router.create_plan)
    assert "UniqueViolationError" in source
    assert "active_plan_for_scope" in source.split("except UniqueViolationError")[1]


def test_the_hourly_limit_leaves_room_to_replace_a_plan():
    """Two limits that individually look fine and together make the feature unusable.

    Set equal to the active cap, a student who filled their three slots and archived one
    could not start the replacement for an hour. Only running the sequence showed it.
    """
    assert store.MAX_PLANS_PER_HOUR > store.MAX_ACTIVE_PLANS
    assert store.MAX_PLANS_PER_DAY >= store.MAX_PLANS_PER_HOUR


def test_a_reservation_in_flight_holds_an_active_slot():
    """The active cap has to count plans that are still being built.

    The plan row is not written until after generation, so a cap that counted only
    `study_plans` was check-then-act all over again: several requests through the lock
    each saw the same empty list and each took a slot. Measured, before this: five plans
    against a cap of three.
    """
    connection = _FakeConnection(
        rows=[{"plan_id": "1", "scope_title": "Gravitation", "updated_at": None}],
        values=[store.MAX_ACTIVE_PLANS - 1],  # the rest of the slots are mid-generation
    )
    with pytest.raises(store.LimitReached) as raised:
        asyncio.run(store.reserve_generation(connection, "student-1", "T", "n"))
    assert raised.value.status == 409


def test_an_abandoned_reservation_stops_holding_a_slot():
    """A process that dies mid-generation must not cost a student a slot for ever."""
    source = inspect.getsource(store.reserve_generation)
    assert "IN_FLIGHT_MINUTES" in source
    assert "outcome = 'started'" in source
    assert store.IN_FLIGHT_MINUTES == 5


def test_no_value_is_ever_written_into_the_sql():
    """CLAUDE.md rule 4, and it says "no exceptions".

    The window was `"5 minutes"` interpolated into `interval '{...}'`. A module constant,
    so not injectable — but the shape only stays safe while nobody makes it configurable,
    and the alternative is one bound argument. Column lists are a different thing: they
    cannot be bound, and interpolating one is this codebase's existing idiom.
    """
    source = inspect.getsource(store.reserve_generation)
    assert "make_interval(mins => $2)" in source
    # The rule is about interpolation, not about the word "interval": `interval '1 hour'`
    # written directly in a query is a SQL literal and is fine. Every SQL string in this
    # module is triple-quoted, so the thing to forbid is a triple-quoted *f-string*. The
    # f-string that builds the refusal message is not SQL and is none of this rule's
    # business.
    assert 'f"""' not in source


def test_an_unplannable_scope_costs_nothing():
    """Five taps on a chapter with no published questions used to cost five model calls
    and all five of a student's hourly slots, and give them nothing.

    The inventory is a read; building it is the cheap half of this route. So it is built
    first, and a scope that cannot produce a plan is refused on the free side of the line.
    """
    source = inspect.getsource(plans_router.create_plan)
    inventory_at = source.index("build_inventory")
    refusal_at = source.index("if not inventory.question_buckets:")
    reserve_at = source.index("reserve_generation")
    generate_at = source.index("await _generate(inventory)")
    assert inventory_at < refusal_at < reserve_at < generate_at


def test_both_refusals_for_an_empty_scope_say_the_same_thing():
    """One fact, one wording — the early refusal and the backstop are the same sentence."""
    source = inspect.getsource(plans_router.create_plan)
    assert source.count("_nothing_to_plan(scope)") == 2
