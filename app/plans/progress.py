"""Whether a step is done, and how far through the plan that puts the student.

The claim Jeene Mode makes is that finishing the plan means something. That is a claim
about evidence, so a step that can be measured is measured: a practice step completes
because the attempt log says the student answered enough of its questions well enough,
not because they tapped it. Only reading and watching are self-marked, because there is
genuinely nothing there to measure and pretending otherwise would be worse than admitting
it.

Two consequences worth being explicit about, because they read as inconsistencies until
you know why:

  * **`study_plan_steps.state` is not the source of truth for a graded step.** It holds
    `pending` or `skipped`, and the real answer is computed here on every read. There is
    no cache to go stale and no second place to be wrong. If this ever gets slow it is a
    rollup table on top, not a column written at answer time.
  * **Latest attempt per question, always.** A question got wrong in March and right in
    April is known. Counting every attempt would mean a student who revised until they
    understood something scores worse than one who guessed right first time.
"""

from __future__ import annotations

import asyncpg

# One row per question the step covers, carrying only the most recent attempt. The
# `DISTINCT ON` ordering is what makes it the most recent, and Postgres's cheapest way
# of saying so.
_LATEST_FOR_QUESTIONS = """
    SELECT DISTINCT ON (a.question_id)
           a.question_id, a.is_correct
      FROM attempts a
     WHERE a.firebase_uid = $1
       AND a.tenant_id = $2
       AND a.question_id = ANY($3::text[])
     ORDER BY a.question_id, a.created_at DESC
"""


class StepProgress:
    """How a step stands. Cheap to build and never stored."""

    __slots__ = ("offered", "answered", "correct", "state")

    def __init__(self, offered: int, answered: int, correct: int, state: str):
        self.offered = offered
        self.answered = answered
        self.correct = correct
        self.state = state

    @property
    def accuracy(self) -> float:
        return round(self.correct / self.answered, 4) if self.answered else 0.0

    @property
    def done(self) -> bool:
        return self.state == "done"

    def __repr__(self) -> str:  # pragma: no cover - debugging only
        return (
            f"StepProgress({self.state}, {self.correct}/{self.answered}"
            f" of {self.offered})"
        )


async def step_progress(
    connection: asyncpg.Connection,
    tenant: str,
    firebase_uid: str,
    step: asyncpg.Record | dict,
    question_ids: list[str],
) -> StepProgress:
    """Where this step stands for this student.

    `question_ids` is the frozen list from the step's items — the caller has it already
    from `resolve.freeze_item`, and passing it in keeps this function free of any opinion
    about how a step's questions were chosen.
    """
    get = step.get if isinstance(step, dict) else step.__getitem__
    stored_state = get("state") or "pending"
    completion = get("completion_kind")

    # A skipped step is skipped whatever the log says, and a self-marked one is whatever
    # the student said. Neither is derived, and neither should be second-guessed here.
    if stored_state == "skipped" or completion == "self":
        return StepProgress(
            offered=len(question_ids), answered=0, correct=0, state=stored_state
        )

    if not question_ids:
        # A graded step whose selector found nothing cannot be completed and must not be
        # reported as complete. It also must not block the plan — JM-4 skips it.
        return StepProgress(offered=0, answered=0, correct=0, state="pending")

    rows = await connection.fetch(
        _LATEST_FOR_QUESTIONS, firebase_uid, tenant, question_ids
    )
    answered = len(rows)
    correct = sum(1 for r in rows if r["is_correct"])
    return StepProgress(
        offered=len(question_ids),
        answered=answered,
        correct=correct,
        state=_state_for(
            answered=answered,
            correct=correct,
            required_questions=get("required_questions"),
            required_accuracy=get("required_accuracy"),
        ),
    )


def _state_for(
    *,
    answered: int,
    correct: int,
    required_questions: int | None,
    required_accuracy: float | None,
) -> str:
    """The three states a graded step can be in.

    A step needs both bars: enough answered, and enough of those right. Either alone is
    gameable — answer everything badly, or answer one thing correctly and stop.
    """
    if answered == 0:
        return "pending"
    if required_questions is None or required_accuracy is None:
        # The schema's `graded_steps_state_their_bar` check makes this unreachable through
        # the tables. Treated as unfinishable rather than complete: a step with no bar has
        # not been passed, whatever was answered.
        return "in_progress"
    if answered >= required_questions and correct / answered >= float(required_accuracy):
        return "done"
    return "in_progress"


def plan_percent(states: list[str]) -> float:
    """How far through the plan the student is.

    Skipped steps leave the sum entirely rather than counting as done. A student who
    skips four of six steps has not finished two thirds of anything, and a bar that says
    otherwise is the kind of number that makes the whole screen untrustworthy.
    """
    countable = [s for s in states if s != "skipped"]
    if not countable:
        return 0.0
    return round(sum(1 for s in countable if s == "done") / len(countable), 4)


def plan_is_complete(states: list[str]) -> bool:
    """Every step that still counts is done — and at least one of them exists.

    A plan whose every step was skipped is not a finished plan, and saying so would put a
    completion on a student's history that they would rightly not recognise.
    """
    countable = [s for s in states if s != "skipped"]
    return bool(countable) and all(s == "done" for s in countable)
