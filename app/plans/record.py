"""Where the student actually stands in this scope, from the attempt log.

The planner is told what the student says about themselves and what they have actually
done, and they often disagree. Both are sent; neither is presented as the truth about
the other.

Two attribution rules, and they are deliberately different:

  * **Coverage and accuracy count every mapping.** A question tagged to two concepts in
    the scope is one question the student has done, and it is reachable by asking for
    either concept — which is exactly what `/concepts/{id}/questions` does today. This
    has to match, or a plan says "practise 10" and the deck holds 6.
  * **Weakness counts only the primary mapping.** A question is *about* one concept and
    touches others; blaming every tagged concept for a miss spreads one gap across
    several and the ranking stops meaning anything. This is what the Mistake Book
    already does, and the two screens must not disagree about who is weak.

Latest attempt per question throughout. A question got wrong in March and right in April
is not an outstanding weakness, and a planner that keeps treating it as one plans the
same remedial step forever.
"""

from __future__ import annotations

import asyncpg

from app.plans.schema import PlacementCheck, StudentRecord, WeakConcept
from app.ranking import worth_doing
from app.visibility import NOT_UNRELEASED_TEST_SQL

# Enough to shape a plan without becoming the plan. Past a handful, a "weak concepts"
# list is just the scope again.
_MAX_WEAK_CONCEPTS = 5

# What the student has done in this scope, one row per question, latest attempt only.
_SCOPE_ATTEMPTS_SQL = f"""
    WITH reachable AS (
        SELECT DISTINCT q.question_id
          FROM questions q
          JOIN question_concept_mappings m ON m.question_id = q.question_id
         WHERE q.tenant_id = $2
           AND q.status = 'published'
           AND m.concept_node_id = ANY($3::text[])
           {NOT_UNRELEASED_TEST_SQL}
    )
    SELECT DISTINCT ON (a.question_id)
           a.question_id, a.is_correct
      FROM attempts a
      JOIN reachable r ON r.question_id = a.question_id
     WHERE a.firebase_uid = $1 AND a.tenant_id = $2
     ORDER BY a.question_id, a.created_at DESC
"""

# Every published question the scope can reach. The denominator of coverage, and the
# honest answer to "how much is there" — bucket totals double-count multi-tagged
# questions and cannot be summed for this.
_SCOPE_TOTAL_SQL = f"""
    SELECT COUNT(DISTINCT q.question_id)
      FROM questions q
      JOIN question_concept_mappings m ON m.question_id = q.question_id
     WHERE q.tenant_id = $1
       AND q.status = 'published'
       AND m.concept_node_id = ANY($2::text[])
       {NOT_UNRELEASED_TEST_SQL}
"""

# Per-concept standing, attributed by primary mapping only. See the module docstring.
_WEAK_CONCEPTS_SQL = f"""
    WITH latest AS (
        SELECT DISTINCT ON (a.question_id)
               a.question_id, a.is_correct
          FROM attempts a
         WHERE a.firebase_uid = $1 AND a.tenant_id = $2
         ORDER BY a.question_id, a.created_at DESC
    )
    SELECT m.concept_node_id AS node_id,
           n.title,
           COUNT(*) AS attempted,
           COUNT(*) FILTER (WHERE NOT l.is_correct) AS wrong
      FROM latest l
      JOIN question_concept_mappings m
           ON m.question_id = l.question_id AND m.is_primary
      JOIN questions q ON q.question_id = l.question_id
      JOIN nodes n ON n.node_id = m.concept_node_id
     WHERE m.concept_node_id = ANY($3::text[])
       AND q.tenant_id = $2
       AND q.status = 'published'
       AND n.tenant_id = $2
       {NOT_UNRELEASED_TEST_SQL}
     GROUP BY m.concept_node_id, n.title
    HAVING COUNT(*) FILTER (WHERE NOT l.is_correct) > 0
"""


# The student's standing on named concepts, one row each. Used for foundation
# candidates, where "has this person actually got a gap here" is the whole question:
# a derived candidate with no evidence behind it should not become a step.
_CONCEPT_STANDING_SQL = f"""
    WITH latest AS (
        SELECT DISTINCT ON (a.question_id)
               a.question_id, a.is_correct
          FROM attempts a
         WHERE a.firebase_uid = $1 AND a.tenant_id = $2
         ORDER BY a.question_id, a.created_at DESC
    )
    SELECT m.concept_node_id AS node_id,
           COUNT(*) AS attempted,
           COUNT(*) FILTER (WHERE l.is_correct) AS correct
      FROM latest l
      JOIN question_concept_mappings m
           ON m.question_id = l.question_id AND m.is_primary
      JOIN questions q ON q.question_id = l.question_id
     WHERE m.concept_node_id = ANY($3::text[])
       AND q.tenant_id = $2
       AND q.status = 'published'
       {NOT_UNRELEASED_TEST_SQL}
     GROUP BY m.concept_node_id
"""


async def scope_question_total(
    connection: asyncpg.Connection, tenant: str, concept_ids: list[str]
) -> int:
    """Distinct published questions the scope can reach."""
    if not concept_ids:
        return 0
    return await connection.fetchval(_SCOPE_TOTAL_SQL, tenant, concept_ids) or 0


async def concept_standing(
    connection: asyncpg.Connection,
    tenant: str,
    firebase_uid: str | None,
    concept_ids: list[str],
) -> dict[str, tuple[int, float]]:
    """`{node_id: (attempted, accuracy)}` for concepts the student has met.

    Absent from the mapping means never attempted, which the planner is told to read as
    "unknown" rather than as "fine": a prerequisite nobody has tested is exactly the one
    worth a placement question.
    """
    if not firebase_uid or not concept_ids:
        return {}
    rows = await connection.fetch(
        _CONCEPT_STANDING_SQL, firebase_uid, tenant, concept_ids
    )
    return {
        r["node_id"]: (r["attempted"], _ratio(r["correct"], r["attempted"]))
        for r in rows
    }


async def build_student_record(
    connection: asyncpg.Connection,
    tenant: str,
    firebase_uid: str | None,
    concept_ids: list[str],
    total_questions: int,
    *,
    self_reported: str | None = None,
    intent: str | None = None,
    placement: PlacementCheck | None = None,
) -> StudentRecord:
    """The student's standing, or an honestly empty record.

    A missing uid is not an error. The admin debug view has no student, and a brand-new
    student has no history — both should produce the same thing, because the planner has
    to handle "I know nothing about this person" either way, and a special case here
    would be a special case the prompt never sees.
    """
    empty = StudentRecord(
        self_reported=self_reported,
        intent=intent,
        placement_check=placement,
        has_history=False,
    )
    if not firebase_uid or not concept_ids:
        return empty

    attempts = await connection.fetch(
        _SCOPE_ATTEMPTS_SQL, firebase_uid, tenant, concept_ids
    )
    if not attempts:
        return empty

    attempted = len(attempts)
    correct = sum(1 for r in attempts if r["is_correct"])

    weak_rows = await connection.fetch(
        _WEAK_CONCEPTS_SQL, firebase_uid, tenant, concept_ids
    )
    weak = sorted(
        (
            WeakConcept(
                node_id=r["node_id"],
                title=r["title"],
                attempted=r["attempted"],
                wrong=r["wrong"],
                accuracy=_ratio(r["attempted"] - r["wrong"], r["attempted"]),
            )
            for r in weak_rows
        ),
        key=lambda w: -worth_doing(w.wrong, w.attempted),
    )[:_MAX_WEAK_CONCEPTS]

    return StudentRecord(
        scope_attempted=attempted,
        scope_correct=correct,
        scope_accuracy=_ratio(correct, attempted),
        scope_coverage=_ratio(attempted, total_questions),
        weak_concepts=weak,
        self_reported=self_reported,
        intent=intent,
        placement_check=placement,
        has_history=True,
    )


def _ratio(part: int, whole: int) -> float:
    return round(part / whole, 4) if whole else 0.0
