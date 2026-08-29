"""The planning contract: what goes to a model, and what has to come back.

The first half is the inventory — everything a planning model is allowed to know, and
nothing else.

These models are the contract, not a convenience. A field that is not here does not
reach a model, and adding one is a reviewed change — read `inventory.py` before you do,
because the reason the list is short is not tidiness.

They live here rather than in `app/schemas.py` because that file is the app's API
contract and none of this is served to the app. The only endpoint that returns an
inventory is the admin debug view, which exists so a teacher can read what the planner
was told before arguing with what it produced.

Everything here is metadata: what exists, how much of it, how the student has done. A
question is described by its facets and counted, never quoted, and never identified —
there are no question ids anywhere in this file, which is what makes it impossible for
a plan to name one.

The second half is `StudyPlanOut`, the shape a plan must have however it was produced.
The deterministic planner in `fallback.py` and the model in JM-5 both emit exactly this,
which is what lets the validator, the persistence layer and the app stay ignorant of
which one wrote the plan they are looking at.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# The vocabulary the planner works in.
#
# `unrated` is for questions the pipeline has not graded. They are real and practisable,
# and hiding them would make a small scope look emptier than it is — so they are counted,
# and a selector may ask for them. What a checkpoint may not do is rest on them alone: it
# claims to measure at a target difficulty, and an ungraded question cannot support that
# claim. `fallback.py` honours that and JM-5's validator enforces it.
BucketDifficulty = Literal["easy", "medium", "hard", "unrated"]
TargetDifficulty = Literal["easy", "medium", "hard"]
Proficiency = Literal["basic", "intermediate", "advanced"]
Intent = Literal["first_time", "revising", "exam_soon"]


class ScopeInfo(BaseModel):
    """What the student asked for help with."""

    node_id: str
    type: Literal["chapter", "topic", "subtopic"]
    title: str
    description: str | None = None
    chapter_id: str | None = None
    chapter_title: str | None = None
    subject: str | None = None
    class_level: int | None = None
    estimated_minutes: int | None = None
    difficulty: str | None = None
    pedagogical_notes: str | None = None


class TeachingNode(BaseModel):
    """A concept or subtopic inside the scope. What a step can point at."""

    node_id: str
    title: str
    description: str | None = None
    difficulty: str | None = None
    estimated_minutes: int | None = None
    parent_id: str | None = None
    prerequisite_node_ids: list[str] = []


class FoundationCandidate(BaseModel):
    """Groundwork from outside the scope that the plan may draw on.

    `source` is the difference between a prerequisite a teacher wrote down and one this
    code guessed from keyword overlap, and the planner is told to treat them differently:
    an authored one is trusted, a derived one needs the student's record to justify it.
    """

    node_id: str
    title: str
    description: str | None = None
    chapter_id: str | None = None
    chapter_title: str | None = None
    source: Literal["authored", "derived_keywords"]
    # How many published questions exist here, so a foundation step is practisable.
    question_count: int = 0
    # The student's standing on this, when there is any. Null means never attempted,
    # which is not the same as doing badly and the planner is told so.
    student_accuracy: float | None = None
    student_attempted: int = 0


class VideoItem(BaseModel):
    youtube_id: str
    title: str
    channel: str = ""
    hangs_on_node_id: str
    hangs_on_title: str
    # True when nothing in the scope has a video of its own and this was inherited from
    # an ancestor. A general lecture is worth offering; pretending it is specific is not.
    inherited: bool = False
    # Null until the admin path backfills it. Guidance must not invent a timestamp.
    duration_seconds: int | None = None


class NotesItem(BaseModel):
    chapter_id: str
    title: str
    page_count: int | None = None


class TestItem(BaseModel):
    test_id: str
    title: str
    duration_minutes: int | None = None
    question_count: int = 0
    # How much of this paper is actually about the scope. Usually small: the live papers
    # are ingested full mocks, not topic tests.
    in_scope_question_count: int = 0


class Materials(BaseModel):
    videos: list[VideoItem] = []
    notes: list[NotesItem] = []
    tests: list[TestItem] = []


class QuestionBucket(BaseModel):
    """Questions that are interchangeable for planning purposes.

    The planner never picks a question. It picks a bucket and a count, and the backend
    resolves that to real questions later. This is the whole reason no question text
    has to cross the boundary.

    `node_id` is a concept, or a subtopic when the scope was large enough to trigger a
    rollup — `InventoryRollup` says which.
    """

    node_id: str
    question_type: str
    difficulty: BucketDifficulty
    total: int
    student_attempted: int = 0
    student_correct: int = 0
    # How many of these have an "Understand with AI" retelling available, so a step can
    # be built around a student who is stuck. The text itself is an answer and never
    # travels; only the count does.
    has_explanations: int = 0


class InventoryRollup(BaseModel):
    """Whether buckets were coarsened, so a thin plan can be explained afterwards."""

    applied: bool = False
    level: Literal["concept", "subtopic"] = "concept"
    original_bucket_count: int = 0


class WeakConcept(BaseModel):
    node_id: str
    title: str
    attempted: int
    wrong: int
    accuracy: float


class PlacementCheck(BaseModel):
    taken: bool = False
    correct: int = 0
    of: int = 0


class StudentRecord(BaseModel):
    """Where this student actually stands, which is often not what they said.

    Every number is derived from the attempt log, latest attempt per question, so a
    question got wrong in March and right in April reads as known.
    """

    scope_attempted: int = 0
    scope_correct: int = 0
    scope_accuracy: float = 0.0
    scope_coverage: float = 0.0
    weak_concepts: list[WeakConcept] = []
    self_reported: Proficiency | None = None
    intent: Intent | None = None
    placement_check: PlacementCheck | None = None
    # False for a signed-out or brand-new student. The planner is told to plan for
    # somebody it knows nothing about rather than to assume they are weak.
    has_history: bool = False


class PlanConstraints(BaseModel):
    min_steps: int = 4
    max_steps: int = 8
    max_foundation_steps: int = 2
    target_difficulty: TargetDifficulty = "medium"
    available_question_types: list[str] = []
    checkpoint_required: bool = True


class Inventory(BaseModel):
    """The whole of what crosses the boundary."""

    scope: ScopeInfo
    concepts: list[TeachingNode] = []
    subtopics: list[TeachingNode] = []
    foundation_candidates: list[FoundationCandidate] = []
    materials: Materials = Materials()
    question_buckets: list[QuestionBucket] = []
    # Distinct published questions in the scope. Bucket totals cannot be summed to get
    # this: a question tagged to two concepts sits in two buckets, which is correct for
    # "what can I draw on for this concept" and wrong for "how much is there".
    scope_question_total: int = 0
    rollup: InventoryRollup = InventoryRollup()
    student: StudentRecord = StudentRecord()
    constraints: PlanConstraints = PlanConstraints()


# ---------------------------------------------------------------------------
# The plan itself: what a planner must produce, whoever the planner is.
# ---------------------------------------------------------------------------

StepKind = Literal["learn", "practise", "verify", "consolidate"]
CompletionKind = Literal["self", "accuracy", "checkpoint"]
SelectorOrder = Literal["easiest_first", "mixed", "hardest_first"]
ItemType = Literal["video", "notes", "test", "questions"]


class QuestionSelector(BaseModel):
    """A filter over questions, which is the only way a plan may refer to them.

    Not a list of ids, and the difference is the whole design. A planner that never
    receives a question cannot name one; the backend resolves this to real questions at
    the moment the student opens the step (JM-3), which also means a plan written in
    March still works in June after the bank has been edited.

    `concept_node_ids` may name a subtopic rather than a concept when the inventory
    rolled its buckets up — the resolver expands any node to its concept descendants, so
    both work and neither needs a special case here.
    """

    concept_node_ids: list[str]
    # Empty means "any", for both of these. A scope whose questions are all ungraded, or
    # all of one type, would otherwise need the planner to enumerate what it found rather
    # than say it does not care.
    question_types: list[str] = []
    difficulty: list[BucketDifficulty] = []
    count: int
    order: SelectorOrder = "mixed"
    # Skip anything the student has already answered. What a checkpoint needs, and what
    # a first-contact step must not do — meeting a question again is how revision works.
    exclude_seen: bool = False


class PlanItem(BaseModel):
    """One thing to open. A named reference, or a selector; never both.

    Named for the few and individually meaningful — a particular lecture, the chapter's
    notes, a specific paper. Filtered for the many and interchangeable.
    """

    type: ItemType
    video_id: str | None = None
    notes_chapter_id: str | None = None
    test_id: str | None = None
    selector: QuestionSelector | None = None


class StepCompletion(BaseModel):
    """How a step is judged done.

    `self` is the only kind a tap can satisfy, and it is allowed only where there is
    genuinely nothing to measure — reading and watching. Everything else is derived from
    the attempt log, so a plan cannot be completed by tapping through it.
    """

    kind: CompletionKind
    required_questions: int | None = None
    required_accuracy: float | None = None


class PlanStep(BaseModel):
    kind: StepKind
    title: str
    # One sentence on why this step, for this student, now.
    why: str
    # The instructions. A step that names material without saying how to use it is a
    # link, and the student already had links.
    how_to_use: list[str]
    focus_node_ids: list[str] = []
    is_foundation: bool = False
    # Indices of earlier steps. Rendered as a rail in v1; stored as a DAG so the graph
    # view later is a rendering change rather than a migration.
    depends_on: list[int] = []
    estimated_minutes: int = 15
    items: list[PlanItem] = []
    completion: StepCompletion


class StudyPlanOut(BaseModel):
    """A whole plan, from either planner."""

    summary: str
    steps: list[PlanStep] = []
