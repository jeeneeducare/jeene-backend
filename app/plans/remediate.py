"""What happens when the checkpoint says no.

The checkpoint is the claim the whole feature rests on: finishing the plan means you
understand the topic. So a miss cannot be waved through, and it cannot be a dead end
either — a student who is told "not yet" and left facing the same eight questions has
been given a locked door, and the only way through it is to answer the questions they
have now seen the answers to. That is not a measurement.

So a miss does two things. It appends work on the concepts the check itself found — not
the record's guess at what is weak, but the specific ideas that were just got wrong — and
it clears the checkpoint so the retake draws questions the student has not met. The plan
grows; nobody starts over; the second check still measures something.

Two limits, both deliberate:

  * **Rounds are capped.** A plan that grows every time it is failed is a plan nobody
    finishes, and at some point the honest answer is that this needs a teacher rather
    than another six questions.
  * **Only concepts with a real miss.** One wrong answer out of one is noise; the
    threshold is on the count of misses, not on accuracy, because a checkpoint is small
    and a percentage of three questions is not a number.
"""

from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from app.plans.guidance import ROLE_KIND, how_to_use
from app.plans.schema import BucketDifficulty, PlanItem, PlanStep, QuestionSelector, StepCompletion

#: How many times a plan will grow itself. Beyond this the checkpoint stays open and the
#: student keeps their attempts — what they do not get is a longer plan.
MAX_ROUNDS = 2

#: Per round. Two is enough to be worth doing and few enough to be worth finishing; a
#: student who missed six concepts is not helped by six more steps.
MAX_STEPS_PER_ROUND = 2

#: Questions in a remediation step. Short on purpose — this is a targeted retry, not the
#: practice step over again.
QUESTIONS_PER_STEP = 6

#: What counts as a concept worth acting on. One wrong answer out of one is not evidence.
MIN_MISSES = 2

#: Below this, a single miss is enough: a checkpoint small enough that two misses would
#: be most of it has nothing left to distinguish.
SMALL_CHECKPOINT = 6

#: The bar for a remediation step. Deliberately gentler than the checkpoint's — the point
#: is to get the idea working, and the checkpoint is still there to decide whether it did.
REQUIRED_ACCURACY = 0.6


@dataclass
class MissedConcept:
    """One concept the checkpoint found, and how badly."""

    concept_node_id: str
    title: str
    missed: int
    answered: int


_MISSED_SQL = """
    WITH latest AS (
        SELECT DISTINCT ON (a.question_id) a.question_id, a.is_correct
          FROM attempts a
         WHERE a.firebase_uid = $1
           AND a.tenant_id = $2
           AND a.question_id = ANY($3::text[])
         ORDER BY a.question_id, a.created_at DESC
    )
    SELECT n.node_id AS concept_node_id,
           n.title,
           count(*) FILTER (WHERE NOT latest.is_correct) AS missed,
           count(*) AS answered
      FROM latest
      JOIN question_concept_mappings m ON m.question_id = latest.question_id
      JOIN nodes n ON n.node_id = m.concept_node_id
     WHERE m.is_primary
       AND n.tenant_id = $2
       AND n.status = 'published'
     GROUP BY n.node_id, n.title
    HAVING count(*) FILTER (WHERE NOT latest.is_correct) > 0
     ORDER BY missed DESC, answered DESC, n.node_id
"""


async def missed_concepts(
    connection: asyncpg.Connection,
    tenant: str,
    firebase_uid: str,
    question_ids: list[str],
) -> list[MissedConcept]:
    """The concepts behind the questions this student got wrong in the checkpoint.

    Latest attempt per question, as everywhere else: a question got wrong and then right
    is a question they now know, and remediating it would be work on something already
    fixed.

    Only the primary concept. A question is often mapped to several, and remediating on
    all of them turns one missed question into three steps about loosely related ideas.
    """
    if not question_ids:
        return []
    rows = await connection.fetch(_MISSED_SQL, firebase_uid, tenant, question_ids)
    return [
        MissedConcept(
            concept_node_id=r["concept_node_id"],
            title=r["title"],
            missed=r["missed"],
            answered=r["answered"],
        )
        for r in rows
    ]


def worth_acting_on(
    missed: list[MissedConcept], checkpoint_size: int
) -> list[MissedConcept]:
    """The ones a step should be built for, most-missed first.

    The threshold relaxes for a small checkpoint. Eight questions across three concepts
    means at most three chances each, and insisting on two misses there would mean a
    student who got a third of the check wrong is told nothing was worth adding.
    """
    floor = 1 if checkpoint_size <= SMALL_CHECKPOINT else MIN_MISSES
    return [m for m in missed if m.missed >= floor][:MAX_STEPS_PER_ROUND]


def steps_for(
    missed: list[MissedConcept],
    *,
    intent: str | None,
    difficulty: list[BucketDifficulty],
) -> list[PlanStep]:
    """One practice step per concept, aimed at exactly what the check found.

    `exclude_seen` is on for the same reason it is on for the checkpoint: the questions
    the student just met are the ones whose answers they have just read.
    """
    steps: list[PlanStep] = []
    for concept in missed:
        steps.append(
            PlanStep(
                kind=ROLE_KIND["remediation"],
                title=f"Fix: {concept.title}",
                why=(
                    f"The check caught {concept.missed} of "
                    f"{concept.answered} on this, so it is the thing standing between "
                    "you and finishing."
                ),
                how_to_use=how_to_use("remediation", intent),
                focus_node_ids=[concept.concept_node_id],
                estimated_minutes=QUESTIONS_PER_STEP * 2,
                items=[
                    PlanItem(
                        type="questions",
                        selector=QuestionSelector(
                            concept_node_ids=[concept.concept_node_id],
                            difficulty=list(difficulty),
                            count=QUESTIONS_PER_STEP,
                            order="easiest_first",
                            exclude_seen=True,
                        ),
                    )
                ],
                completion=StepCompletion(
                    kind="accuracy",
                    required_questions=QUESTIONS_PER_STEP,
                    required_accuracy=REQUIRED_ACCURACY,
                ),
            )
        )
    return steps
