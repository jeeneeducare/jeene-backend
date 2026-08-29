"""Reading and writing plans. The only module that touches the three plan tables.

Nothing here decides what a good plan looks like — `fallback.py` does that, and JM-5's
model will. This turns a `StudyPlanOut` into rows and back, and it owns the two rules
that are about a student's history rather than about pedagogy: how many plans they may
have running, and how often they may ask for a new one.

Both of those are counted in the database rather than in memory. Render runs more than
one worker and restarts them freely, so an in-process counter would be a limit that
resets whenever the platform felt like it — and the handoff-code limiter in `tests.py`
is per-process for a reason that does not apply here: that one is throttling guesses at
a six-character code over a one-minute window, where a restart only forgives. This one
is a spending limit on a paid call, and it has to hold across workers and across days.
"""

from __future__ import annotations

import uuid

import asyncpg

from app.plans.schema import StudyPlanOut

# How many plans a student may have running at once. The designed home screen shows
# three, and an uncapped list stops being a list of plans and becomes a list of
# abandoned intentions.
MAX_ACTIVE_PLANS = 3

# What a student may spend. Generating is the only paid path — resuming, opening and
# completing are free and unlimited — so this is the only place a limit belongs.
MAX_PLANS_PER_DAY = 10
MAX_PLANS_PER_HOUR = 3

# Columns the plan list and the plan detail both need. Named rather than starred so a
# column added later has to be asked for.
_PLAN_COLUMNS = """
    plan_id, firebase_uid, tenant_id, scope_node_id, scope_type, scope_title,
    proficiency, intent, origin, provider, model, prompt_version,
    accuracy_at_generation, status, summary, subject, completed_at, created_at,
    updated_at
"""


async def active_plan_for_scope(
    connection: asyncpg.Connection, firebase_uid: str, scope_node_id: str
):
    """The plan already running for this scope, if there is one.

    Asking twice must resume rather than fork: a student who taps "Plan this" again on a
    topic they started last week should find their finished steps, not a fresh plan that
    makes the work look undone. The partial unique index enforces this at the table; this
    is the read that makes it a feature rather than an error.
    """
    return await connection.fetchrow(
        f"""
        SELECT {_PLAN_COLUMNS} FROM study_plans
         WHERE firebase_uid = $1 AND scope_node_id = $2 AND status = 'active'
        """,
        firebase_uid,
        scope_node_id,
    )


async def list_plans(connection: asyncpg.Connection, firebase_uid: str, limit: int = 50):
    return await connection.fetch(
        f"""
        SELECT {_PLAN_COLUMNS} FROM study_plans
         WHERE firebase_uid = $1
         ORDER BY (status = 'active') DESC, updated_at DESC
         LIMIT $2
        """,
        firebase_uid,
        limit,
    )


async def get_plan(connection: asyncpg.Connection, plan_id: str, firebase_uid: str):
    """One plan, scoped to its owner.

    The uid is in the WHERE rather than checked afterwards, so a plan belonging to
    somebody else is indistinguishable from one that does not exist.
    """
    return await connection.fetchrow(
        f"""
        SELECT {_PLAN_COLUMNS} FROM study_plans
         WHERE plan_id = $1::uuid AND firebase_uid = $2
        """,
        str(plan_id),
        firebase_uid,
    )


async def active_plans(connection: asyncpg.Connection, firebase_uid: str):
    return await connection.fetch(
        """
        SELECT plan_id, scope_title, updated_at FROM study_plans
         WHERE firebase_uid = $1 AND status = 'active'
         ORDER BY updated_at
        """,
        firebase_uid,
    )


async def recent_plan_counts(
    connection: asyncpg.Connection, firebase_uid: str
) -> tuple[int, int]:
    """(created in the last hour, created in the last day).

    Counts every plan ever created in the window, whatever became of it. Counting only
    active ones would make "create, archive, repeat" a way around the limit, which is
    exactly the loop a bored student finds first.
    """
    row = await connection.fetchrow(
        """
        SELECT COUNT(*) FILTER (WHERE created_at > now() - interval '1 hour') AS hour,
               COUNT(*) FILTER (WHERE created_at > now() - interval '1 day')  AS day
          FROM study_plans WHERE firebase_uid = $1
        """,
        firebase_uid,
    )
    return row["hour"], row["day"]


async def steps_for(connection: asyncpg.Connection, plan_id) -> list:
    return await connection.fetch(
        """
        SELECT step_id, plan_id, position, kind, title, why, how_to_use,
               focus_node_ids, is_foundation, depends_on, estimated_minutes,
               completion_kind, required_questions, required_accuracy, state,
               completed_at
          FROM study_plan_steps
         WHERE plan_id = $1::uuid
         ORDER BY position
        """,
        str(plan_id),
    )


async def items_for(connection: asyncpg.Connection, step_ids: list) -> list:
    if not step_ids:
        return []
    return await connection.fetch(
        """
        SELECT item_id, step_id, position, item_type, ref_node_id, ref_id,
               sel_concept_ids, sel_types, sel_difficulty, sel_count, sel_order,
               sel_exclude_seen, resolved_question_ids, resolved_at
          FROM study_plan_step_items
         WHERE step_id = ANY($1::uuid[])
         ORDER BY step_id, position
        """,
        [str(s) for s in step_ids],
    )


async def save_plan(
    connection: asyncpg.Connection,
    *,
    firebase_uid: str,
    tenant: str,
    scope_node_id: str,
    scope_type: str,
    scope_title: str,
    proficiency: str,
    intent: str,
    origin: str,
    prompt_version: int,
    accuracy_at_generation: float | None,
    plan: StudyPlanOut,
    subject: str | None = None,
    provider: str | None = None,
    model: str | None = None,
):
    """Write a whole plan. One transaction, or none of it.

    A plan half-written is worse than no plan: the student sees steps that stop
    mid-sequence with no checkpoint, which is exactly the shape of a plan they have
    already finished. So the plan, its steps and their items go in together.
    """
    plan_id = uuid.uuid4()
    step_ids = [uuid.uuid4() for _ in plan.steps]

    async with connection.transaction():
        await connection.execute(
            """
            INSERT INTO study_plans (
                plan_id, firebase_uid, tenant_id, scope_node_id, scope_type, scope_title,
                proficiency, intent, origin, provider, model, prompt_version,
                accuracy_at_generation, summary, subject
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
            """,
            plan_id, firebase_uid, tenant, scope_node_id, scope_type, scope_title,
            proficiency, intent, origin, provider, model, prompt_version,
            accuracy_at_generation, plan.summary, subject,
        )

        for position, step in enumerate(plan.steps):
            await connection.execute(
                """
                INSERT INTO study_plan_steps (
                    step_id, plan_id, position, kind, title, why, how_to_use,
                    focus_node_ids, is_foundation, depends_on, estimated_minutes,
                    completion_kind, required_questions, required_accuracy
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
                """,
                step_ids[position], plan_id, position, step.kind, step.title, step.why,
                list(step.how_to_use), list(step.focus_node_ids), step.is_foundation,
                # The planner speaks in step indices because it is writing a list; the
                # table speaks in ids because it is storing a graph. Translated here,
                # which is the only place that knows both.
                [step_ids[i] for i in step.depends_on if 0 <= i < len(step_ids)],
                step.estimated_minutes, step.completion.kind,
                step.completion.required_questions, step.completion.required_accuracy,
            )
            for item_position, item in enumerate(step.items):
                selector = item.selector
                await connection.execute(
                    """
                    INSERT INTO study_plan_step_items (
                        item_id, step_id, position, item_type, ref_node_id, ref_id,
                        sel_concept_ids, sel_types, sel_difficulty, sel_count,
                        sel_order, sel_exclude_seen
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                    """,
                    uuid.uuid4(), step_ids[position], item_position, item.type,
                    None,
                    item.video_id or item.notes_chapter_id or item.test_id,
                    list(selector.concept_node_ids) if selector else None,
                    list(selector.question_types) if selector else None,
                    list(selector.difficulty) if selector else None,
                    selector.count if selector else None,
                    selector.order if selector else None,
                    bool(selector.exclude_seen) if selector else False,
                )

    return plan_id


async def set_step_state(
    connection: asyncpg.Connection, step_id, plan_id, state: str
) -> None:
    await connection.execute(
        """
        UPDATE study_plan_steps
           SET state = $3,
               completed_at = CASE WHEN $3 = 'done' THEN now() ELSE completed_at END
         WHERE step_id = $1::uuid AND plan_id = $2::uuid
        """,
        str(step_id),
        str(plan_id),
        state,
    )
    await touch(connection, plan_id)


async def set_plan_status(connection: asyncpg.Connection, plan_id, status: str) -> None:
    await connection.execute(
        """
        UPDATE study_plans
           SET status = $2,
               completed_at = CASE WHEN $2 = 'completed' THEN now() ELSE completed_at END,
               updated_at = now()
         WHERE plan_id = $1::uuid
        """,
        str(plan_id),
        status,
    )


async def touch(connection: asyncpg.Connection, plan_id) -> None:
    """Move a plan up the history. The list is ordered by when it was last worked on."""
    await connection.execute(
        "UPDATE study_plans SET updated_at = now() WHERE plan_id = $1::uuid",
        str(plan_id),
    )


# Every step of these plans with the student's answers against its frozen questions.
#
# One query, because the history screen derives every step of every plan and doing that a
# step at a time is a query per step to draw a list. The shape is the same one
# `progress.py` uses for a single step — latest attempt per question — just grouped.
_STEP_STATES_SQL = """
    WITH plan_steps AS (
        SELECT s.plan_id, s.step_id, s.position, s.state, s.completion_kind,
               s.required_questions, s.required_accuracy
          FROM study_plan_steps s
         WHERE s.plan_id = ANY($1::uuid[])
    ),
    step_questions AS (
        SELECT ps.step_id, q AS question_id
          FROM plan_steps ps
          JOIN study_plan_step_items i ON i.step_id = ps.step_id
          CROSS JOIN LATERAL unnest(i.resolved_question_ids) AS q
    ),
    latest AS (
        SELECT DISTINCT ON (a.question_id) a.question_id, a.is_correct
          FROM attempts a
         WHERE a.firebase_uid = $2
           AND a.tenant_id = $3
           AND a.question_id IN (SELECT question_id FROM step_questions)
         ORDER BY a.question_id, a.created_at DESC
    ),
    counted AS (
        SELECT sq.step_id,
               COUNT(l.question_id) AS answered,
               COUNT(*) FILTER (WHERE l.is_correct) AS correct,
               COUNT(*) AS offered
          FROM step_questions sq
          LEFT JOIN latest l ON l.question_id = sq.question_id
         GROUP BY sq.step_id
    )
    SELECT ps.plan_id, ps.step_id, ps.position, ps.state, ps.completion_kind,
           ps.required_questions, ps.required_accuracy,
           COALESCE(c.answered, 0) AS answered,
           COALESCE(c.correct, 0)  AS correct,
           COALESCE(c.offered, 0)  AS offered
      FROM plan_steps ps
      LEFT JOIN counted c ON c.step_id = ps.step_id
     ORDER BY ps.plan_id, ps.position
"""


async def step_states(
    connection: asyncpg.Connection, plan_ids: list, firebase_uid: str, tenant: str
) -> list:
    """Raw counts per step. `progress.state_from_counts` turns them into states."""
    if not plan_ids:
        return []
    return await connection.fetch(
        _STEP_STATES_SQL, [str(p) for p in plan_ids], firebase_uid, tenant
    )
