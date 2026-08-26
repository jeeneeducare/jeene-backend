"""Figures for questions, and the rule about which of them a student may see.

This lives on its own because the rule is a correctness rule and there is more than one
caller. Before it, `content.py` and `tests.py` each had their own copy of the query, and
they disagreed: `tests.py` filtered out the worked solution's diagrams and `content.py` did
not, so the browse path handed a student the answer. A third copy was about to be written
in `attempts.py`. One implementation, one place to get the rule right.

A worked solution's diagram is part of the answer. For some questions it IS the answer:
one of test 15's questions is answered by the very diagram its solution draws.
"""

from __future__ import annotations

import asyncpg

from app.schemas import QuestionFigure

# What a student may see before the answer is revealed.
STUDENT_VISIBLE_PLACEMENTS = ("stem", "option")
# The worked solution's own diagrams, served only with the solution.
SOLUTION_PLACEMENTS = ("explanation",)
# Diagrams belonging to the "Understand with AI" retelling, served only with that text.
AI_EXPLANATION_PLACEMENTS = ("ai_explanation",)
# Everything. Only for a context where the answer is already known: a submitted paper's
# review, where the sitting is over and there is nothing left to give away.
ALL_PLACEMENTS = STUDENT_VISIBLE_PLACEMENTS + SOLUTION_PLACEMENTS + AI_EXPLANATION_PLACEMENTS


async def fetch_figures(
    connection: asyncpg.Connection,
    question_ids: list[str],
    placements: tuple[str, ...] = STUDENT_VISIBLE_PLACEMENTS,
) -> dict[str, list[QuestionFigure]]:
    """Figures for these questions, restricted to the given placements, in display order.

    The placement filter is the whole point of the signature, and the default is the
    strictest one: a caller allowed to see more has to say so. Anything not named,
    including a placement this code has never heard of, is withheld. Fail closed.

    Tenant scoping is the caller's, not this function's: `question_figures` has no
    tenant_id of its own, so a figure belongs to whichever tenant owns its question.
    Every caller today reaches here with ids from a query filtered on
    `questions.tenant_id`, which is what makes that safe. If you call this with ids
    from anywhere else, scope them first (CLAUDE.md rule 6).
    """
    if not question_ids:
        return {}
    rows = await connection.fetch(
        """
        SELECT question_id, image_url, placement, option_id, caption, width, height
        FROM question_figures
        WHERE question_id = ANY($1::text[]) AND placement = ANY($2::text[])
        ORDER BY question_id, placement, option_id NULLS FIRST, display_order
        """,
        question_ids,
        list(placements),
    )
    figures: dict[str, list[QuestionFigure]] = {}
    for r in rows:
        figures.setdefault(r["question_id"], []).append(
            QuestionFigure(
                image_url=r["image_url"],
                placement=r["placement"],
                option_id=r["option_id"],
                caption=r["caption"],
                width=r["width"],
                height=r["height"],
            )
        )
    return figures
