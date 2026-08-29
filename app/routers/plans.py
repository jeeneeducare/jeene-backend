"""Study plans: the whole feature, server-side, with no model involved.

Every endpoint here works today against the deterministic planner in `fallback.py`. That
is the point of the ordering: the schema, the resolution, the completion arithmetic and
both clients can all be built and reviewed before a single provider call exists, and when
one does exist it slots in at exactly one line of `_generate`. It also means the feature
degrades to something real rather than to an error when the model is unavailable.

The admin debug view came first (JM-1) and stays, because reading what a planner is told
is a different job from reading what it produced.
"""

from __future__ import annotations

from functools import lru_cache

import asyncpg
from asyncpg.exceptions import UniqueViolationError
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth import current_tenant, require_admin, require_user
from app.config import settings
from app.db import get_connection
from app.plans import store
from app.plans.generate import generate
from app.plans.prompt import PROMPT_VERSION
from app.plans.inventory import build_inventory
from app.plans.progress import (
    plan_is_complete,
    plan_percent,
    state_from_counts,
    step_progress,
)
from app.plans.resolve import freeze_item
from app.plans.schema import (
    Intent,
    Inventory,
    PlacementCheck,
    Proficiency,
    QuestionSelector,
)
from app.plans.scope import SCOPE_TYPES, resolve_scope
from app.plans.resolve import resolve_selector
from app.providers.openai_provider import build_provider
from app.questions import fetch_questions_by_ids
from app.schemas import (
    CheckpointResult,
    Question,
    PlanCreate,
    PlanDetail,
    PlanStep,
    PlanStepItem,
    PlanSummary,
    StepItems,
)

router = APIRouter(prefix="/plans", tags=["plans"])

# How many questions the optional placement check asks. Five is short enough that a
# student will actually take it and long enough to tell "never seen this" from "fine".
PLACEMENT_QUESTIONS = 5


# --- reading -------------------------------------------------------------------------


@router.get("", response_model=list[PlanSummary])
async def list_plans(
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[PlanSummary]:
    """The student's plans, active first, then most recently worked on.

    Summaries only — no steps, and therefore no per-step progress query. The history
    screen shows a percentage per plan, so that one number is computed from the step
    states in a single query rather than by opening every plan.
    """
    rows = await store.list_plans(connection, user["uid"])
    if not rows:
        return []
    by_plan, _ = await _derived_states(
        connection, [r["plan_id"] for r in rows], user["uid"], tenant
    )
    return [_summary(r, by_plan.get(r["plan_id"], [])) for r in rows]


@router.get("/placement", response_model=list[Question])
async def placement_check(
    node_id: str = Query(..., description="The scope the student is about to plan."),
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[Question]:
    """A handful of questions to place a student who is not sure, or who has no record.

    Offered rather than required, and deliberately not a plan step: it exists to settle a
    disagreement between what a student says about themselves and what their answers say,
    before either becomes the basis of six steps of work. Graded by the ordinary
    `POST /attempts` path like anything else, so it also seeds the record it is measuring.

    Easiest first and unseen where possible, because a placement check that opens with
    the hardest question in the chapter measures nerve rather than knowledge.
    """
    scope = await resolve_scope(connection, node_id, tenant)
    if scope is None:
        raise HTTPException(status_code=404, detail=_no_scope(node_id))
    ids = await resolve_selector(
        connection,
        tenant,
        user["uid"],
        QuestionSelector(
            concept_node_ids=scope.concept_ids or [node_id],
            count=PLACEMENT_QUESTIONS,
            order="easiest_first",
            exclude_seen=True,
        ),
        salt=node_id,
    )
    return await fetch_questions_by_ids(connection, tenant, ids)


@router.get("/{plan_id}", response_model=PlanDetail)
async def get_plan(
    plan_id: str,
    request: Request,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> PlanDetail:
    """One plan with its steps, and each step's state derived from the attempt log.

    Only the current step's questions are frozen here, never the whole plan. That is not
    a performance choice: the checkpoint asks for questions the student has not seen, and
    freezing it at plan-open time would draw that list *before* they work through steps
    two to five — so the checkpoint would be full of questions they are about to meet.
    A checkpoint has to be resolved late or it is not measuring anything.
    """
    plan = await store.get_plan(connection, plan_id, user["uid"])
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    steps = await store.steps_for(connection, plan["plan_id"])
    items = await store.items_for(connection, [s["step_id"] for s in steps])
    items_by_step: dict = {}
    for item in items:
        items_by_step.setdefault(item["step_id"], []).append(item)

    _, by_step = await _derived_states(
        connection, [plan["plan_id"]], user["uid"], tenant
    )

    # The current step is the first that is neither done nor skipped. Its questions are
    # frozen now so the student meets a real count on the card they are about to tap.
    current = next(
        (
            s
            for s in steps
            if by_step.get(s["step_id"], ("pending", 0, 0))[0]
            in ("pending", "in_progress")
        ),
        None,
    )
    if current is not None:
        for item in items_by_step.get(current["step_id"], []):
            await freeze_item(connection, tenant, user["uid"], item)
        items_by_step[current["step_id"]] = await store.items_for(
            connection, [current["step_id"]]
        )
        frozen = _frozen_ids(items_by_step[current["step_id"]])
        # Now that the real count is known, the step's bar cannot ask for more than that.
        lowered = await store.clamp_required_questions(
            connection, current["step_id"], len(frozen)
        )
        if lowered is not None:
            current = dict(current) | {"required_questions": lowered}
        progress = await step_progress(
            connection,
            tenant,
            user["uid"],
            current,
            frozen,
        )
        by_step[current["step_id"]] = (
            progress.state,
            progress.answered,
            progress.correct,
        )

    rendered = [
        _step(step, items_by_step.get(step["step_id"], []),
              by_step.get(step["step_id"], ("pending", 0, 0)), request)
        for step in steps
    ]
    detail = _summary(plan, [s.state for s in rendered])
    return PlanDetail(**detail.model_dump(), steps=rendered)


@router.get("/{plan_id}/steps/{step_id}/items", response_model=StepItems)
async def step_items(
    plan_id: str,
    step_id: str,
    request: Request,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> StepItems:
    """Everything needed to open this step, with its questions frozen.

    The questions come back in full here rather than as ids, so opening a step is one
    request. They carry stem and option figures and no answer, exactly as the practice
    deck's own endpoint does.
    """
    step = await _owned_step(connection, plan_id, step_id, user["uid"])
    items = await store.items_for(connection, [step["step_id"]])
    for item in items:
        await freeze_item(connection, tenant, user["uid"], item)
    items = await store.items_for(connection, [step["step_id"]])

    question_ids = _frozen_ids(items)
    # Same clamp as the plan read, because either can be the first to freeze this step.
    await store.clamp_required_questions(
        connection, step["step_id"], len(question_ids)
    )
    # No progress read here on purpose. The client refreshes the plan when it closes the
    # sheet, which is where the updated "4 of 8 right" belongs; computing it now would be
    # a query for a number that is stale by the time the student answers anything.
    await store.touch(connection, plan_id)
    return StepItems(
        step_id=str(step["step_id"]),
        items=[_item(i, request) for i in items],
        questions=await fetch_questions_by_ids(connection, tenant, question_ids),
    )


# --- writing -------------------------------------------------------------------------


@router.post("", response_model=PlanDetail, status_code=201)
async def create_plan(
    body: PlanCreate,
    request: Request,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> PlanDetail:
    """Plan a scope, or hand back the plan already running for it.

    Resuming is the common case and must not cost anything: a student who taps "Plan
    this" again on a topic they started last week wants their finished steps, not a fresh
    plan that makes the work look undone. So the existing-plan check comes before both
    limits — resuming is neither a new plan nor a spend.
    """
    existing = await store.active_plan_for_scope(
        connection, user["uid"], body.scope_node_id
    )
    if existing is not None:
        return await get_plan(
            str(existing["plan_id"]), request, user, tenant, connection
        )

    await _check_limits(connection, user["uid"])

    scope = await resolve_scope(connection, body.scope_node_id, tenant)
    if scope is None:
        raise HTTPException(status_code=404, detail=_no_scope(body.scope_node_id))

    inventory = await build_inventory(
        connection,
        scope,
        tenant,
        firebase_uid=user["uid"],
        proficiency=body.proficiency,
        intent=body.intent,
        placement=_placement(body),
    )
    plan, origin, provider, model = await _generate(inventory)
    if not plan.steps:
        # Nothing published under this scope. A plan row with no steps is worse than an
        # error: it sits in the history looking like work the student failed to do.
        raise HTTPException(
            status_code=409,
            detail=(
                f"There is nothing published under '{scope.node['title']}' to build a "
                "plan from yet."
            ),
        )

    try:
        plan_id = await store.save_plan(
            connection,
            firebase_uid=user["uid"],
            tenant=tenant,
            scope_node_id=scope.node["node_id"],
            scope_type=scope.node["type"],
            scope_title=scope.node["title"],
            proficiency=body.proficiency,
            intent=body.intent,
            origin=origin,
            prompt_version=PROMPT_VERSION,
            accuracy_at_generation=inventory.student.scope_accuracy,
            plan=plan,
            subject=inventory.scope.subject,
            provider=provider,
            model=model,
        )
    except UniqueViolationError:
        # Two taps, two workers, one scope. The partial unique index is what makes
        # "plan this" idempotent, and losing that race is a resume rather than an error:
        # the student asked for a plan for this scope and there is now exactly one.
        existing = await store.active_plan_for_scope(
            connection, user["uid"], body.scope_node_id
        )
        if existing is None:
            raise
        return await get_plan(
            str(existing["plan_id"]), request, user, tenant, connection
        )
    return await get_plan(str(plan_id), request, user, tenant, connection)


@router.post("/{plan_id}/steps/{step_id}/complete", response_model=PlanDetail)
async def complete_step(
    plan_id: str,
    step_id: str,
    request: Request,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> PlanDetail:
    """Mark a reading or watching step done.

    Refused for anything gradeable, and that refusal is the feature. The plan's claim is
    that finishing it means something; a graded step that could be tapped done would make
    the whole plan tappable, and the checkpoint at the end would be measuring nobody.
    """
    step = await _owned_step(connection, plan_id, step_id, user["uid"])
    if step["completion_kind"] != "self":
        raise HTTPException(
            status_code=422,
            detail=(
                "This step is judged by the questions you answer, not by marking it. "
                "Answer at least "
                f"{step['required_questions']} of them to complete it."
            ),
        )
    await store.set_step_state(connection, step["step_id"], plan_id, "done")
    return await _refresh(connection, plan_id, request, user, tenant)


@router.post("/{plan_id}/steps/{step_id}/skip", response_model=PlanDetail)
async def skip_step(
    plan_id: str,
    step_id: str,
    request: Request,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> PlanDetail:
    """Skip a step. It leaves the percentage entirely rather than counting as done.

    The checkpoint cannot be skipped. Everything else in a plan is a route to it, and a
    plan finished by skipping the only step that measures anything is a plan that proves
    nothing — which is precisely the claim the feature is built on.
    """
    step = await _owned_step(connection, plan_id, step_id, user["uid"])
    if step["completion_kind"] == "checkpoint":
        raise HTTPException(
            status_code=422,
            detail=(
                "The final check cannot be skipped — it is the part that tells you "
                "whether the rest worked."
            ),
        )
    await store.set_step_state(connection, step["step_id"], plan_id, "skipped")
    return await _refresh(connection, plan_id, request, user, tenant)


@router.post("/{plan_id}/checkpoint/submit", response_model=CheckpointResult)
async def submit_checkpoint(
    plan_id: str,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> CheckpointResult:
    """Close the checkpoint and report what it showed.

    Nothing is graded here — the answers were graded by `POST /attempts` as they were
    given. This reads the result, and completes the plan when everything else is done.

    A miss does not fail the plan. In JM-10 it appends work on the concepts that were
    missed; until then the step simply stays open, which is the same outcome one round
    later and never a worse one.
    """
    plan = await store.get_plan(connection, plan_id, user["uid"])
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")

    steps = await store.steps_for(connection, plan["plan_id"])
    checkpoint = next(
        (s for s in steps if s["completion_kind"] == "checkpoint"), None
    )
    if checkpoint is None:
        raise HTTPException(status_code=404, detail="This plan has no checkpoint")

    items = await store.items_for(connection, [checkpoint["step_id"]])
    progress = await step_progress(
        connection, tenant, user["uid"], checkpoint, _frozen_ids(items)
    )

    by_plan, by_step = await _derived_states(
        connection, [plan["plan_id"]], user["uid"], tenant
    )
    # The checkpoint's own state comes from the read above, which was taken after its
    # items were frozen; the bulk query may have been computed from the same rows but is
    # re-stated here so the two can never disagree about the step being submitted.
    by_step[checkpoint["step_id"]] = (
        progress.state, progress.answered, progress.correct
    )
    states = [
        by_step.get(s["step_id"], ("pending", 0, 0))[0] for s in steps
    ]

    status = plan["status"]
    if plan_is_complete(states) and status == "active":
        await store.set_plan_status(connection, plan_id, "completed")
        status = "completed"
    else:
        await store.touch(connection, plan_id)

    return CheckpointResult(
        plan_id=str(plan["plan_id"]),
        step_id=str(checkpoint["step_id"]),
        offered=progress.offered,
        answered=progress.answered,
        correct=progress.correct,
        accuracy=progress.accuracy,
        required_accuracy=(
            float(checkpoint["required_accuracy"])
            if checkpoint["required_accuracy"] is not None
            else None
        ),
        passed=progress.state == "done",
        plan_status=status,
    )


@router.post("/{plan_id}/archive", response_model=PlanSummary)
async def archive_plan(
    plan_id: str,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> PlanSummary:
    plan = await store.get_plan(connection, plan_id, user["uid"])
    if plan is None:
        raise HTTPException(status_code=404, detail="Plan not found")
    await store.set_plan_status(connection, plan_id, "archived")
    refreshed = await store.get_plan(connection, plan_id, user["uid"])
    by_plan, _ = await _derived_states(
        connection, [refreshed["plan_id"]], user["uid"], tenant
    )
    return _summary(refreshed, by_plan.get(refreshed["plan_id"], []))


# --- admin ----------------------------------------------------------------------------


@router.get("/debug/inventory", response_model=Inventory)
async def debug_inventory(
    node_id: str = Query(..., description="A chapter, topic or subtopic node id."),
    proficiency: Proficiency | None = Query(
        default=None, description="Shapes only `constraints.target_difficulty`."
    ),
    intent: Intent | None = Query(default=None),
    admin: dict = Depends(require_admin),
    connection: asyncpg.Connection = Depends(get_connection),
) -> Inventory:
    """Exactly what a planning model would be sent for this scope, with no student.

    The tenant comes from the admin's own row, never from the query, so an admin of one
    tenant cannot read another's catalogue by asking.

    There is deliberately no way to ask for a named student's record here. The obvious
    convenience — a `?as_student=` uid so a reviewer could see a real plan's inputs —
    would turn a content-admin row, which today grants "may attach a video", into a way
    to read any student's per-concept performance. That is a different permission and it
    should be argued for on its own, not acquire itself as a debugging affordance.
    """
    tenant = admin["tenant_id"]
    scope = await resolve_scope(connection, node_id, tenant)
    if scope is None:
        raise HTTPException(status_code=404, detail=_no_scope(node_id))
    return await build_inventory(
        connection, scope, tenant, firebase_uid=None,
        proficiency=proficiency, intent=intent,
    )


# --- helpers --------------------------------------------------------------------------


async def _generate(inventory: Inventory):
    """Produce a plan. The provider goes above the deterministic planner, never instead.

    The seam JM-4 left. Everything about choosing, calling, validating and repairing lives
    in `plans/generate.py`; this reads the configuration and hands it over.
    """
    result = await generate(
        inventory,
        _planner_provider(),
        enabled=settings.jeene_planner_enabled,
    )
    return result.plan, result.origin, result.provider, result.model


@lru_cache(maxsize=1)
def _planner_provider():
    """Built once. The client holds a connection pool and rebuilding it per request
    would spend more time on TLS than on the plan."""
    return build_provider(settings.openai_api_key, settings.jeene_planner_model)


async def _check_limits(connection: asyncpg.Connection, firebase_uid: str) -> None:
    """The two limits on creating a plan, both counted in the database.

    Render runs more than one worker and restarts them freely, so an in-process counter
    would be a limit that resets whenever the platform felt like it.
    """
    active = await store.active_plans(connection, firebase_uid)
    if len(active) >= store.MAX_ACTIVE_PLANS:
        oldest = active[0]
        raise HTTPException(
            status_code=409,
            detail=(
                f"You already have {len(active)} plans on the go. Archive one first — "
                f"'{oldest['scope_title']}' is the one you have not touched in longest."
            ),
        )

    hour, day = await store.recent_plan_counts(connection, firebase_uid)
    if hour >= store.MAX_PLANS_PER_HOUR:
        raise HTTPException(
            status_code=429,
            detail="That is a lot of new plans in an hour. Try again a bit later.",
        )
    if day >= store.MAX_PLANS_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail="You have started a lot of plans today. Try again tomorrow.",
        )


async def _owned_step(
    connection: asyncpg.Connection, plan_id: str, step_id: str, firebase_uid: str
):
    """A step of a plan the caller owns, or a 404 that says nothing about which failed."""
    step = await connection.fetchrow(
        """
        SELECT s.* FROM study_plan_steps s
          JOIN study_plans p ON p.plan_id = s.plan_id
         WHERE s.step_id = $1::uuid AND s.plan_id = $2::uuid AND p.firebase_uid = $3
        """,
        str(step_id),
        str(plan_id),
        firebase_uid,
    )
    if step is None:
        raise HTTPException(status_code=404, detail="Step not found")
    return step


async def _derived_states(
    connection: asyncpg.Connection, plan_ids: list, firebase_uid: str, tenant: str
) -> tuple[dict, dict]:
    """Real step states for these plans, in one query.

    Returns `({plan_id: [state, ...]}, {step_id: (state, answered, correct)})` — the
    first for percentages, the second for rendering a step.

    Derived rather than read off `study_plan_steps.state`, and derived in bulk rather
    than per step. Both matter: reading the column would make the history screen disagree
    with the plan screen about the same plan, and deriving one step at a time would make
    drawing the history a query per step.
    """
    rows = await store.step_states(connection, plan_ids, firebase_uid, tenant)
    by_plan: dict = {}
    by_step: dict = {}
    for row in rows:
        state = state_from_counts(
            stored_state=row["state"] or "pending",
            completion_kind=row["completion_kind"],
            answered=row["answered"],
            correct=row["correct"],
            required_questions=row["required_questions"],
            required_accuracy=row["required_accuracy"],
        )
        by_plan.setdefault(row["plan_id"], []).append(state)
        by_step[row["step_id"]] = (state, row["answered"], row["correct"])
    return by_plan, by_step


async def _refresh(connection, plan_id, request, user, tenant) -> PlanDetail:
    detail = await get_plan(plan_id, request, user, tenant, connection)
    if plan_is_complete([s.state for s in detail.steps]) and detail.status == "active":
        await store.set_plan_status(connection, plan_id, "completed")
        detail.status = "completed"
    return detail


def _frozen_ids(items) -> list[str]:
    ids: list[str] = []
    for item in items:
        ids.extend(item["resolved_question_ids"] or [])
    return ids


def _summary(plan, states: list[str]) -> PlanSummary:
    return PlanSummary(
        plan_id=str(plan["plan_id"]),
        scope_node_id=plan["scope_node_id"],
        scope_type=plan["scope_type"],
        scope_title=plan["scope_title"],
        proficiency=plan["proficiency"],
        intent=plan["intent"],
        status=plan["status"],
        summary=plan["summary"],
        subject=plan["subject"],
        step_count=len(states),
        done_count=sum(1 for s in states if s == "done"),
        percent=plan_percent(states),
        created_at=plan["created_at"],
        updated_at=plan["updated_at"],
    )


def _step(step, items, derived, request: Request) -> PlanStep:
    state, answered, correct = derived
    return PlanStep(
        step_id=str(step["step_id"]),
        position=step["position"],
        kind=step["kind"],
        title=step["title"],
        why=step["why"],
        how_to_use=list(step["how_to_use"] or []),
        focus_node_ids=list(step["focus_node_ids"] or []),
        is_foundation=step["is_foundation"],
        depends_on=[str(d) for d in (step["depends_on"] or [])],
        estimated_minutes=step["estimated_minutes"],
        completion_kind=step["completion_kind"],
        required_questions=step["required_questions"],
        required_accuracy=(
            float(step["required_accuracy"])
            if step["required_accuracy"] is not None
            else None
        ),
        state=state,
        answered=answered,
        correct=correct,
        items=[_item(i, request) for i in items],
    )


def _item(item, request: Request) -> PlanStepItem:
    resolved = list(item["resolved_question_ids"] or [])
    return PlanStepItem(
        item_id=str(item["item_id"]),
        type=item["item_type"],
        video_id=item["ref_id"] if item["item_type"] == "video" else None,
        player_url=(
            f"{str(request.base_url).rstrip('/')}/player?v={item['ref_id']}"
            if item["item_type"] == "video"
            else None
        ),
        notes_chapter_id=item["ref_id"] if item["item_type"] == "notes" else None,
        test_id=item["ref_id"] if item["item_type"] == "test" else None,
        planned_count=item["sel_count"],
        # Null until frozen, and never guessed from `sel_count`: a step that says ten and
        # opens on six is a bug the student can see.
        question_count=len(resolved) if item["resolved_at"] is not None else None,
        question_ids=resolved,
    )


def _placement(body: PlanCreate) -> PlacementCheck | None:
    if body.placement_of is None:
        return None
    return PlacementCheck(
        taken=True, correct=body.placement_correct or 0, of=body.placement_of
    )


def _no_scope(node_id: str) -> str:
    return (
        f"No published node '{node_id}' to plan for. A plan needs one of: "
        f"{', '.join(SCOPE_TYPES)}."
    )
