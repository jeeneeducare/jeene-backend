"""Study plans. For now, only the window onto what a planner would be told.

The generating endpoints arrive in JM-4. This ships first and alone on purpose: the
inventory is the whole of the content guarantee and most of the plan's quality, and both
are things a person has to read rather than infer from a plan that came out badly. So the
catalogue is reviewable before anything consumes it.

Admin-only, because it is a view of the bank's shape — how many questions of each kind
sit under each concept — which is not a student's business even though nothing in it is
an answer.
"""

from __future__ import annotations

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth import require_admin
from app.db import get_connection
from app.plans.inventory import build_inventory
from app.plans.schema import Inventory, Intent, Proficiency
from app.plans.scope import SCOPE_TYPES, resolve_scope

router = APIRouter(prefix="/plans", tags=["plans"])


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

    The catalogue is the reviewable half anyway: which material exists, how much of it,
    and how it is spread across the tree. The student half is arithmetic over the attempt
    log, and JM-4's real endpoint exercises it for the caller themselves.
    """
    tenant = admin["tenant_id"]
    scope = await resolve_scope(connection, node_id, tenant)
    if scope is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No published node '{node_id}' to plan for. A plan needs one of: "
                f"{', '.join(SCOPE_TYPES)}."
            ),
        )
    return await build_inventory(
        connection,
        scope,
        tenant,
        firebase_uid=None,
        proficiency=proficiency,
        intent=intent,
    )
