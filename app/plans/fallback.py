"""The deterministic planner: a real plan, built from rules, with no model involved.

Not a stub and not a degraded mode. It is what runs whenever no provider is configured,
whenever one times out, and whenever a model's output fails validation twice — and since
`JEENE_PLANNER_ENABLED` ships as false, it is what every student gets on day one.

Writing it first is deliberate. It forces the pedagogy into code that can be read and
argued with, rather than leaving it as an instruction in a prompt that nobody can test.
It is also the standard the model's output is reviewed against: a generated plan that is
not better than this one is not worth the call.

What it will not do is notice things. A model can see that a student is strong on the
first half of a chapter and weak on the second and shape the plan around it. This applies
a template. That gap is the reason JM-5 exists, and it is worth being clear-eyed that the
template covers the ordinary case rather than the interesting one.

Nothing here touches the database. It takes an `Inventory` and returns a `StudyPlanOut`,
which makes every rule in it testable against a hand-written catalogue.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.plans.guidance import ROLE_KIND, Role, how_to_use
from app.plans.schema import (
    Inventory,
    PlanItem,
    PlanStep,
    QuestionSelector,
    StepCompletion,
    StudyPlanOut,
)

# What a scope with any material to work with reaches. Not a guarantee, and it would be
# dishonest to make it one: a scope with nothing but a dozen ungraded MCQs and no lecture
# genuinely supports two steps — practise, then check — and padding it to three would mean
# inventing work. `_MAX_STEPS` is the real constraint, and it is a judgement about what
# gets finished rather than a measurement.
_TARGET_MIN_STEPS = 3
_MAX_STEPS = 6

# What counts as evidence of a real gap in a prerequisite. Below five attempts there is
# not enough to say anything, and a student who is at 50% on something they have met is
# not reliably weak at it — they are learning it.
_FOUNDATION_MIN_ATTEMPTS = 5
_FOUNDATION_MAX_ACCURACY = 0.5

# A step that opens on two questions reads as broken, however honest the number is.
_MIN_STEP_QUESTIONS = 3

# Exam-shape only earns a step if there is a real set of past questions behind it.
_MIN_PYQS = 5

# Roughly how long a question takes, for the estimate on a step. Deliberately coarse:
# `expected_time_seconds` exists per question but the planner is working from counts, so
# a per-question average is the honest resolution.
_MINUTES_PER_QUESTION = 2


@dataclass
class _Candidate:
    """A step the template would like to include, and how badly."""

    role: Role
    step: PlanStep
    # Higher wins when the plan is over length. The checkpoint and first contact are
    # mandatory and never compete.
    priority: int
    mandatory: bool = False


def plan(inventory: Inventory) -> StudyPlanOut:
    """Build a plan for this inventory. Always returns something usable."""
    intent = inventory.student.intent
    target = inventory.constraints.target_difficulty
    nodes = _planning_node_ids(inventory)

    candidates: list[_Candidate] = []
    for build in (
        _foundation,
        _orient,
        _first_contact,
        _weak_spots,
        _exam_shape,
        _consolidate,
    ):
        candidate = build(inventory, nodes, target, intent)
        if candidate is not None:
            candidates.append(candidate)

    checkpoint = _checkpoint(inventory, nodes, target, intent)
    if checkpoint is not None:
        candidates.append(checkpoint)

    steps = _trim(candidates)
    _chain(steps)
    return StudyPlanOut(summary=_summary(inventory, steps), steps=steps)


# --- the steps ---------------------------------------------------------------------


def _foundation(inventory, nodes, target, intent) -> _Candidate | None:
    """Groundwork, and only on evidence.

    A plan that opens with an earlier chapter every time is a plan that tells every
    student they are behind. So this needs the record to say so: enough attempts to mean
    something, and an accuracy low enough that it is a gap rather than a wobble. A
    candidate the student has never touched is not evidence of anything, and is skipped —
    a placement check is the right way to ask about those, not a step.
    """
    gaps = [
        c
        for c in inventory.foundation_candidates
        if c.student_attempted >= _FOUNDATION_MIN_ATTEMPTS
        and c.student_accuracy is not None
        and c.student_accuracy < _FOUNDATION_MAX_ACCURACY
        and c.question_count >= _MIN_STEP_QUESTIONS
    ]
    if not gaps:
        return None

    weakest = min(gaps, key=lambda c: c.student_accuracy or 0.0)
    count = min(5, weakest.question_count)
    selector = QuestionSelector(
        concept_node_ids=[weakest.node_id],
        question_types=inventory.constraints.available_question_types,
        difficulty=["easy", "medium"],
        count=count,
        order="easiest_first",
    )
    where = f" from {weakest.chapter_title}" if weakest.chapter_title else ""
    return _Candidate(
        role="foundation",
        priority=90,
        step=PlanStep(
            kind=ROLE_KIND["foundation"],
            title=f"Shore up {weakest.title}",
            why=(
                f"{inventory.scope.title} leans on this{where}, and your answers there "
                "are coming out wrong more often than not."
            ),
            how_to_use=how_to_use("foundation", intent),
            focus_node_ids=[weakest.node_id],
            is_foundation=True,
            estimated_minutes=count * _MINUTES_PER_QUESTION + 5,
            items=[PlanItem(type="questions", selector=selector)],
            completion=StepCompletion(
                kind="accuracy", required_questions=count, required_accuracy=0.6
            ),
        ),
    )


def _orient(inventory, nodes, target, intent) -> _Candidate | None:
    """One thing to read or watch first. Skipped entirely when there is neither.

    Prefers a video that actually hangs in the scope over one inherited from the chapter
    above, and falls back to the chapter's notes. A step that offers both would be a step
    most students do neither half of.
    """
    videos = inventory.materials.videos
    own = [v for v in videos if not v.inherited]
    chosen = (own or videos or [None])[0]

    if chosen is not None:
        minutes = (
            max(5, round(chosen.duration_seconds / 60))
            if chosen.duration_seconds
            else 15
        )
        return _Candidate(
            role="orient",
            priority=60,
            step=PlanStep(
                kind=ROLE_KIND["orient"],
                title="Get the picture first",
                why=(
                    "A pass over the whole idea before you start answering, so the "
                    "questions are recognition rather than guesswork."
                ),
                how_to_use=how_to_use("orient", intent),
                focus_node_ids=[chosen.hangs_on_node_id],
                estimated_minutes=minutes,
                items=[PlanItem(type="video", video_id=chosen.youtube_id)],
                completion=StepCompletion(kind="self"),
            ),
        )

    notes = inventory.materials.notes
    if not notes:
        return None
    pages = notes[0].page_count or 0
    return _Candidate(
        role="orient",
        priority=60,
        step=PlanStep(
            kind=ROLE_KIND["orient"],
            title="Read it through once",
            why=(
                "There is no lecture for this yet, so the chapter's notes are the way in."
            ),
            how_to_use=how_to_use("orient", intent),
            focus_node_ids=[inventory.scope.node_id],
            estimated_minutes=max(10, min(40, pages * 3)) if pages else 20,
            items=[PlanItem(type="notes", notes_chapter_id=notes[0].chapter_id)],
            completion=StepCompletion(kind="self"),
        ),
    )


def _first_contact(inventory, nodes, target, intent) -> _Candidate | None:
    """The first real work. Mandatory whenever the scope has any questions at all.

    Deliberately easier than the target: the point is to get moving and to find out what
    is already there, not to measure. `exclude_seen` stays off — meeting a question again
    is how revision works, and for a student with history this is where that starts.
    """
    available = _available(inventory, nodes, difficulty=["easy", "unrated"])
    difficulty = ["easy", "unrated"]
    if available < _MIN_STEP_QUESTIONS:
        # A scope with nothing easy is common in a PYQ-only chapter. Take what is there
        # rather than dropping the step the whole plan is built on.
        available = _available(inventory, nodes)
        difficulty = []
    if available < _MIN_STEP_QUESTIONS:
        return None

    count = _clamp(8, available, inventory)
    required = max(_MIN_STEP_QUESTIONS, count - 2)
    return _Candidate(
        role="first_contact",
        priority=100,
        mandatory=True,
        step=PlanStep(
            kind=ROLE_KIND["first_contact"],
            title="Find your feet",
            why=(
                "A gentle set across the whole of "
                f"{inventory.scope.title}, to show you where you already stand."
            ),
            how_to_use=how_to_use("first_contact", intent),
            focus_node_ids=nodes,
            estimated_minutes=count * _MINUTES_PER_QUESTION,
            items=[
                PlanItem(
                    type="questions",
                    selector=QuestionSelector(
                        concept_node_ids=nodes,
                        question_types=inventory.constraints.available_question_types,
                        difficulty=difficulty,
                        count=count,
                        order="easiest_first",
                    ),
                )
            ],
            completion=StepCompletion(
                kind="accuracy", required_questions=required, required_accuracy=0.6
            ),
        ),
    )


def _weak_spots(inventory, nodes, target, intent) -> _Candidate | None:
    """The concepts the record says are shakiest. Nothing to aim at without a record."""
    weak = inventory.student.weak_concepts[:2]
    if not weak:
        return None
    weak_ids = [w.node_id for w in weak]
    available = _available(inventory, weak_ids)
    if available < _MIN_STEP_QUESTIONS:
        return None

    count = _clamp(8, available, inventory)
    titles = " and ".join(w.title for w in weak)
    # One concept takes a singular verb. Worth the branch: this sentence is shown to a
    # student, and a plan that cannot write English does not read as one worth following.
    verb = "account" if len(weak) > 1 else "accounts"
    return _Candidate(
        role="weak_spots",
        priority=95,
        step=PlanStep(
            kind=ROLE_KIND["weak_spots"],
            title="Go at what is not sticking",
            why=(
                f"{titles} {verb} for more of your wrong answers here than "
                "anything else."
            ),
            how_to_use=how_to_use("weak_spots", intent),
            focus_node_ids=weak_ids,
            estimated_minutes=count * _MINUTES_PER_QUESTION,
            items=[
                PlanItem(
                    type="questions",
                    selector=QuestionSelector(
                        concept_node_ids=weak_ids,
                        question_types=inventory.constraints.available_question_types,
                        difficulty=[target],
                        count=count,
                        order="mixed",
                    ),
                )
            ],
            completion=StepCompletion(
                kind="accuracy",
                required_questions=max(_MIN_STEP_QUESTIONS, count - 2),
                required_accuracy=0.6,
            ),
        ),
    )


def _exam_shape(inventory, nodes, target, intent) -> _Candidate | None:
    """Real past questions, if there is a real set of them."""
    if "pyq" not in inventory.constraints.available_question_types:
        return None
    available = _available(inventory, nodes, types=["pyq"])
    if available < _MIN_PYQS:
        return None

    count = _clamp(10, available, inventory)
    return _Candidate(
        role="exam_shape",
        priority=70,
        step=PlanStep(
            kind=ROLE_KIND["exam_shape"],
            title="See how it is actually asked",
            why=(
                "Past questions combine ideas rather than testing one, which is the part "
                "practice on its own never prepares you for."
            ),
            how_to_use=how_to_use("exam_shape", intent),
            focus_node_ids=nodes,
            estimated_minutes=count * _MINUTES_PER_QUESTION,
            items=[
                PlanItem(
                    type="questions",
                    selector=QuestionSelector(
                        concept_node_ids=nodes,
                        question_types=["pyq"],
                        difficulty=[target],
                        count=count,
                        order="mixed",
                    ),
                )
            ],
            completion=StepCompletion(
                kind="accuracy",
                required_questions=max(_MIN_STEP_QUESTIONS, count - 3),
                required_accuracy=0.6,
            ),
        ),
    )


def _consolidate(inventory, nodes, target, intent) -> _Candidate | None:
    """A last read before the checkpoint. Only worth a step when notes exist."""
    notes = inventory.materials.notes
    if not notes:
        return None
    return _Candidate(
        role="consolidate",
        priority=40,
        step=PlanStep(
            kind=ROLE_KIND["consolidate"],
            title="Tidy it up before the check",
            why="A short pass over what you have been getting wrong, before it is measured.",
            how_to_use=how_to_use("consolidate", intent),
            focus_node_ids=[inventory.scope.node_id],
            estimated_minutes=15,
            items=[PlanItem(type="notes", notes_chapter_id=notes[0].chapter_id)],
            completion=StepCompletion(kind="self"),
        ),
    )


def _checkpoint(inventory, nodes, target, intent) -> _Candidate | None:
    """The measurement the plan builds to.

    Unseen questions at the target difficulty, which is the only version of this that
    means anything: a checkpoint drawn from questions the student has already answered
    measures memory of a deck, not understanding of a topic.

    Ungraded questions are allowed in only as a fallback for a thin scope. A checkpoint
    claims to measure at a difficulty, and an ungraded question cannot support the claim
    — so it is a last resort rather than a filler.
    """
    available = _available(inventory, nodes, difficulty=[target])
    difficulty = [target]
    if available < _MIN_STEP_QUESTIONS:
        available = _available(inventory, nodes, difficulty=[target, "unrated"])
        difficulty = [target, "unrated"]
    if available < _MIN_STEP_QUESTIONS:
        available = _available(inventory, nodes)
        difficulty = []
    if available < _MIN_STEP_QUESTIONS:
        return None

    count = _clamp(10, available, inventory)
    return _Candidate(
        role="checkpoint",
        priority=100,
        mandatory=True,
        step=PlanStep(
            kind=ROLE_KIND["checkpoint"],
            title=f"Check you have {inventory.scope.title}",
            why=(
                "Questions you have not seen, at the level you are aiming for. This is "
                "what finishing the plan means."
            ),
            how_to_use=how_to_use("checkpoint", intent),
            focus_node_ids=nodes,
            estimated_minutes=count * _MINUTES_PER_QUESTION,
            items=[
                PlanItem(
                    type="questions",
                    selector=QuestionSelector(
                        concept_node_ids=nodes,
                        question_types=inventory.constraints.available_question_types,
                        difficulty=difficulty,
                        count=count,
                        order="mixed",
                        exclude_seen=True,
                    ),
                )
            ],
            completion=StepCompletion(
                kind="checkpoint",
                required_questions=max(_MIN_STEP_QUESTIONS, count - 2),
                required_accuracy=0.7,
            ),
        ),
    )


# --- assembly ----------------------------------------------------------------------

# The order a plan reads in, which is not the order the steps were considered in.
_SEQUENCE: list[Role] = [
    "foundation",
    "orient",
    "first_contact",
    "weak_spots",
    "exam_shape",
    "consolidate",
    "checkpoint",
]


def _trim(candidates: list[_Candidate]) -> list[PlanStep]:
    """Cut to length by priority, then put what survives into pedagogical order."""
    keep = [c for c in candidates if c.mandatory]
    optional = sorted(
        (c for c in candidates if not c.mandatory),
        key=lambda c: -c.priority,
    )
    keep += optional[: max(0, _MAX_STEPS - len(keep))]
    keep.sort(key=lambda c: _SEQUENCE.index(c.role))
    return [c.step for c in keep]


def _chain(steps: list[PlanStep]) -> None:
    """A straight line through the steps.

    Stored as a DAG because the shape is worth keeping, but the template has no branches
    to express: every step here depends on the one before it. A model may do better.
    """
    for index in range(1, len(steps)):
        steps[index].depends_on = [index - 1]


def _summary(inventory: Inventory, steps: list[PlanStep]) -> str:
    if not steps:
        # Nothing published under this scope. JM-4 turns this into a refusal rather than
        # storing it, but the sentence has to be honest in case one ever reaches a screen.
        return f"There is nothing published under {inventory.scope.title} to work from yet."
    minutes = sum(s.estimated_minutes for s in steps)
    hours, rest = divmod(minutes, 60)
    if hours and rest:
        length = f"about {hours}h {rest}m"
    elif hours:
        length = f"about {hours}h"
    else:
        length = f"about {rest} minutes"
    return f"{len(steps)} steps through {inventory.scope.title}, {length} of work."


# --- counting ----------------------------------------------------------------------


def _planning_node_ids(inventory: Inventory) -> list[str]:
    """The node ids the buckets are keyed by.

    Concepts normally; subtopics when the inventory rolled up. Selectors have to use the
    same granularity the counts came from, or a step asks for questions against ids the
    catalogue said nothing about.
    """
    if inventory.rollup.applied:
        return [n.node_id for n in inventory.subtopics]
    return [n.node_id for n in inventory.concepts]


def _available(
    inventory: Inventory,
    node_ids: list[str],
    *,
    types: list[str] | None = None,
    difficulty: list[str] | None = None,
) -> int:
    """How many questions the buckets say are behind a filter.

    An upper bound, not a count: a question tagged to two of these concepts is in two
    buckets. That is fine for deciding whether a step is worth including, and it is why
    `_clamp` also holds every step under the scope's true distinct total. The authoritative
    clamp happens in JM-3, against real rows, when the step is first opened.
    """
    wanted = set(node_ids)
    return sum(
        b.total
        for b in inventory.question_buckets
        if b.node_id in wanted
        and (not types or b.question_type in types)
        and (not difficulty or b.difficulty in difficulty)
    )


def _clamp(wanted: int, available: int, inventory: Inventory) -> int:
    """Never ask for more than could exist.

    Held under `scope_question_total` as well as the bucket sum, because the bucket sum
    over-counts and a step that promises ten questions and opens on six is a bug the
    student can see.
    """
    ceiling = available
    if inventory.scope_question_total:
        ceiling = min(ceiling, inventory.scope_question_total)
    return max(_MIN_STEP_QUESTIONS, min(wanted, ceiling))
