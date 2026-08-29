"""Fetching a known set of questions, as a student may see them.

A plan step holds a frozen list of question ids, and so does a placement check. Both need
the same thing: those exact questions, with their stem and option figures and nothing
else. Written once here because two routers need it and because the rule it has to honour
— never the answer, never a question from an unreleased paper — is not one to re-derive
per caller. `figures.py` is the cautionary tale.

This does **not** decide whether the caller is allowed to ask for these ids. That is the
caller's job and it is a real one: an endpoint that fetches whatever ids it is handed is
a way to enumerate the bank. `plans.py` scopes them to the caller's own plans.
"""

from __future__ import annotations

import asyncpg

from app.figures import fetch_figures
from app.schemas import Question
from app.visibility import NOT_UNRELEASED_TEST_SQL

# Named columns, and the answer-bearing ones are conspicuously absent. Same discipline as
# `app/plans/inventory.py`, for a different reader: that one keeps content from a model,
# this one keeps the answer from a student before they have earned it.
_BY_IDS_SQL = f"""
    SELECT q.question_id, q.question_type, q.question_text, q.options_json, q.difficulty,
           (SELECT array_agg(m.concept_node_id)
              FROM question_concept_mappings m
             WHERE m.question_id = q.question_id) AS concept_ids
      FROM questions q
     WHERE q.tenant_id = $1
       AND q.question_id = ANY($2::text[])
       AND q.status = 'published'
       {NOT_UNRELEASED_TEST_SQL}
"""


async def fetch_questions_by_ids(
    connection: asyncpg.Connection, tenant: str, question_ids: list[str]
) -> list[Question]:
    """These questions, in the order asked for, skipping any that no longer qualify.

    Order is preserved because a step's frozen list *is* an order — easiest first, or a
    deliberate spread — and returning them in database order would quietly discard the
    only sequencing decision the plan made.

    A question that has since been unpublished, or that has been pulled into an unreleased
    paper, simply does not come back. The step then shows fewer questions than its frozen
    list, which is the honest outcome: the alternative is a card that opens on nothing.
    """
    if not question_ids:
        return []

    rows = await connection.fetch(_BY_IDS_SQL, tenant, question_ids)
    figures = await fetch_figures(connection, [r["question_id"] for r in rows])
    by_id = {
        r["question_id"]: Question(
            question_id=r["question_id"],
            question_type=r["question_type"],
            question_text=r["question_text"],
            options=r["options_json"],
            difficulty=r["difficulty"],
            figures=figures.get(r["question_id"], []),
            concept_ids=list(r["concept_ids"] or []),
        )
        for r in rows
    }
    return [by_id[qid] for qid in question_ids if qid in by_id]
