"""Turning a plan's filters into real questions, once, on this side of the boundary.

A plan describes what to practise; it never names a question. That is what keeps the
question bank away from the planner, and it means somebody has to do the naming — this
module, at the moment a student first opens a step.

**Once** is the important word. The ids are written back to the item and reused forever
after, because a deck that reshuffles between sittings makes "6 of 8 right" meaningless:
the six the student answered on Tuesday are not the eight they are looking at on
Thursday. The selector is the plan and survives content edits; the frozen list is the
sitting and does not move.

Two rules that look small and are not:

  * **A selector may name a subtopic, not just a concept.** When the inventory rolled its
    buckets up, that is the granularity the planner was counting in. So every node id is
    expanded to its concept descendants here, and a selector written either way resolves
    the same.
  * **Under-supply is a clamp, never an error.** A step that asked for ten and found six
    shows six. What it must never do is show "10 questions" and open on six, so the count
    a client renders comes from the frozen list and never from `sel_count`.
"""

from __future__ import annotations

import logging

import asyncpg

from app.plans.schema import QuestionSelector
from app.visibility import NOT_UNRELEASED_TEST_SQL

logger = logging.getLogger(__name__)

# How a step's questions are laid out. The values are a closed set from the plan schema
# and never come from a client, but they are still mapped through this table rather than
# interpolated, so that the only strings that can reach an ORDER BY are these ones.
#
# `mixed` is a deterministic hash rather than `random()`: resolution has to be repeatable
# for the same inputs, or a re-resolve after a content edit churns the whole deck and two
# people debugging the same step see different questions. Hashing the id also avoids the
# clustering you get from ordering by id, where questions from one source sit together.
#
# The hash is salted with the item id, which matters more than it looks. Unsalted, every
# student asking for "eight mixed medium PYQs on this concept" gets the same eight, for
# ever: a bank of forty questions would have eight of them doing all the work while the
# other thirty-two are never seen, and a student who redoes a topic meets the same deck.
# Salting spreads the load across the bank and still resolves identically every time for
# a given step, which is what freezing needs.
_ORDERINGS = {
    "easiest_first": "difficulty_rank ASC, q.calibrated_difficulty ASC NULLS FIRST, q.question_id",
    "hardest_first": "difficulty_rank DESC, q.calibrated_difficulty DESC NULLS LAST, q.question_id",
    "mixed": "md5($8 || q.question_id)",
}

# `unrated` is the bucket name for a question the pipeline has not graded. It is NULL in
# the column, so the filter has to say so explicitly rather than comparing to a string.
_UNRATED = "unrated"

# The salt only exists for the ordering that uses it, so the argument list is built
# from the rendered query rather than assumed.
_SALT_PLACEHOLDER = "$8"

_DIFFICULTY_RANK = """
    CASE q.difficulty
        WHEN 'easy' THEN 1 WHEN 'medium' THEN 2 WHEN 'hard' THEN 3 ELSE 4
    END
"""


def _resolution_query(
    selector: QuestionSelector,
    tenant: str,
    firebase_uid: str | None,
    salt: str,
) -> tuple[str, list]:
    """Build the query for this selector's shape, and the arguments that go with it.

    Assembled rather than written out once because the ordering clause and the
    seen-questions ordering are structural, not values — but every fragment that reaches
    the string comes from a constant in this module, and every actual value is a bound
    parameter. Nothing a client or a model wrote is interpolated.

    The arguments are built here rather than by the caller, because they cannot be built
    correctly anywhere else: the salt placeholder only exists for one of the orderings,
    and a caller passing a fixed argument list got that wrong the first time it was
    written. asyncpg checks the count, so the mistake is a runtime failure on two of the
    three orderings — the kind a fake connection will never catch.
    """
    order = _ORDERINGS[selector.order]
    args = [
        tenant,
        selector.concept_node_ids,
        firebase_uid,
        selector.question_types or None,
        [d for d in selector.difficulty if d != _UNRATED] or None,
        _UNRATED in selector.difficulty,
        selector.count,
    ]
    if _SALT_PLACEHOLDER in order:
        args.append(salt)
    if selector.exclude_seen:
        # Unseen first, then the ones seen longest ago. A step that asked for ten unseen
        # questions in a scope with six left should still hand over ten — the four it
        # tops up with are the four the student is most likely to have forgotten.
        order = f"(last_seen IS NOT NULL), last_seen ASC, {order}"

    return (
        f"""
        WITH RECURSIVE targets AS (
            SELECT n.node_id, n.type
              FROM nodes n
             WHERE n.tenant_id = $1 AND n.node_id = ANY($2::text[])
               AND n.status = 'published'
            UNION ALL
            SELECT child.node_id, child.type
              FROM nodes child
              JOIN targets t ON child.parent_id = t.node_id
             WHERE child.tenant_id = $1 AND child.status = 'published'
        ),
        concepts AS (
            SELECT DISTINCT node_id FROM targets WHERE type = 'concept'
        ),
        candidates AS (
            SELECT DISTINCT q.question_id, q.difficulty, q.calibrated_difficulty,
                   {_DIFFICULTY_RANK} AS difficulty_rank,
                   (SELECT MAX(a.created_at) FROM attempts a
                     WHERE a.question_id = q.question_id AND a.firebase_uid = $3
                   ) AS last_seen
              FROM questions q
              JOIN question_concept_mappings m ON m.question_id = q.question_id
              JOIN concepts c ON c.node_id = m.concept_node_id
             WHERE q.tenant_id = $1
               AND q.status = 'published'
               AND ($4::text[] IS NULL OR q.question_type = ANY($4::text[]))
               AND ($5::text[] IS NULL OR q.difficulty = ANY($5::text[])
                    OR ($6::bool AND q.difficulty IS NULL))
               {NOT_UNRELEASED_TEST_SQL}
        )
        SELECT question_id FROM candidates q
         ORDER BY {order}
         LIMIT $7
    """,
        args,
    )


async def resolve_selector(
    connection: asyncpg.Connection,
    tenant: str,
    firebase_uid: str | None,
    selector: QuestionSelector,
    salt: str = "",
) -> list[str]:
    """The questions this selector describes, in the order the step should show them.

    `salt` varies which slice of an over-large pool comes back, without making the result
    unrepeatable. `freeze_item` passes the item id; a caller previewing a selector can
    leave it empty and get a stable answer.
    """
    if not selector.concept_node_ids or selector.count <= 0:
        return []

    sql, args = _resolution_query(selector, tenant, firebase_uid, salt)
    rows = await connection.fetch(sql, *args)
    return [r["question_id"] for r in rows]


async def freeze_item(
    connection: asyncpg.Connection,
    tenant: str,
    firebase_uid: str | None,
    item: asyncpg.Record | dict,
) -> list[str]:
    """The frozen question list for this item, resolving it the first time only.

    Idempotent, and safe against two devices opening the same step at once: the write is
    conditional on nothing having been written yet, and a caller that loses the race reads
    the winner's list rather than its own. Both students see the same deck, which is the
    entire point of freezing.
    """
    already = item.get("resolved_question_ids") if isinstance(item, dict) else item["resolved_question_ids"]
    if already:
        return list(already)

    selector = selector_from_row(item)
    if selector is None:
        return []

    resolved = await resolve_selector(
        connection, tenant, firebase_uid, selector, salt=_item_id(item)
    )
    if not resolved:
        # Nothing matched. Deliberately not written back: the scope may simply have been
        # unpublished for a moment, and freezing an empty list would make that permanent.
        logger.warning(
            "Selector for item %s resolved to nothing", _item_id(item)
        )
        return []

    if len(resolved) < selector.count:
        # Not an error. Recorded rather than stored: `sel_count` is what was asked for and
        # the frozen array is what exists, so the shortfall is already in the row and any
        # client that renders the array length is telling the truth.
        logger.info(
            "Item %s asked for %d questions and found %d",
            _item_id(item), selector.count, len(resolved),
        )

    stored = await connection.fetchval(
        """
        UPDATE study_plan_step_items
           SET resolved_question_ids = $2, resolved_at = now()
         WHERE item_id = $1::uuid AND resolved_at IS NULL
        RETURNING resolved_question_ids
        """,
        _item_id(item),
        resolved,
    )
    if stored is not None:
        return list(stored)

    # Somebody else froze it between the read and the write. Theirs wins.
    winner = await connection.fetchval(
        "SELECT resolved_question_ids FROM study_plan_step_items WHERE item_id = $1::uuid",
        _item_id(item),
    )
    return list(winner or [])


def selector_from_row(item: asyncpg.Record | dict) -> QuestionSelector | None:
    """Rebuild a selector from its stored columns, or None for a named reference."""
    get = item.get if isinstance(item, dict) else item.__getitem__
    try:
        if get("item_type") != "questions":
            return None
    except (KeyError, IndexError):
        return None
    count = get("sel_count")
    if not count:
        return None
    return QuestionSelector(
        concept_node_ids=list(get("sel_concept_ids") or []),
        question_types=list(get("sel_types") or []),
        difficulty=list(get("sel_difficulty") or []),
        count=count,
        order=get("sel_order") or "mixed",
        exclude_seen=bool(get("sel_exclude_seen")),
    )


def _item_id(item) -> str:
    """The item's id as text, so the ::uuid casts above accept either form."""
    return str(item.get("item_id") if isinstance(item, dict) else item["item_id"])
