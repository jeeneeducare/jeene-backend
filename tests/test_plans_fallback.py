"""The deterministic planner, against four catalogues it has to cope with.

These are golden tests on shape and ordering, never on wording. The prose is the part
most likely to be rewritten by a teacher, and a test that pins it would make every
improvement look like a regression.

The four inventories are the cases that actually occur: a chapter with everything, a
subtopic with nothing but questions, a student nobody knows anything about, and a student
with a real hole in an earlier chapter.
"""

import pytest

from app.plans import fallback
from app.plans.guidance import GUIDANCE, ROLE_KIND, how_to_use
from app.plans.schema import (
    FoundationCandidate,
    Intent,
    Inventory,
    InventoryRollup,
    Materials,
    NotesItem,
    PlanConstraints,
    QuestionBucket,
    ScopeInfo,
    StepKind,
    StudentRecord,
    TeachingNode,
    VideoItem,
    WeakConcept,
)

CONCEPTS = ["c_height", "c_depth", "c_latitude"]


def _buckets(types=("mcq", "pyq"), difficulties=("easy", "medium", "hard"), total=6):
    return [
        QuestionBucket(
            node_id=node, question_type=qtype, difficulty=difficulty, total=total
        )
        for node in CONCEPTS
        for qtype in types
        for difficulty in difficulties
    ]


def _inventory(
    *,
    buckets=None,
    videos=(),
    notes=(),
    student=None,
    candidates=(),
    target="medium",
    types=("mcq", "pyq"),
    scope_total=60,
    rollup=None,
) -> Inventory:
    return Inventory(
        scope=ScopeInfo(
            node_id="phy_11_ch8_g_variation",
            type="subtopic",
            title="Acceleration due to gravity",
            chapter_id="phy_11_ch8",
            chapter_title="Gravitation",
            subject="phy",
            class_level=11,
        ),
        concepts=[
            TeachingNode(node_id=c, title=c.replace("c_", "g with "))
            for c in CONCEPTS
        ],
        subtopics=[
            TeachingNode(node_id="phy_11_ch8_g_variation", title="Acceleration due to gravity")
        ],
        foundation_candidates=list(candidates),
        materials=Materials(videos=list(videos), notes=list(notes)),
        question_buckets=_buckets() if buckets is None else buckets,
        scope_question_total=scope_total,
        rollup=rollup or InventoryRollup(),
        student=student or StudentRecord(),
        constraints=PlanConstraints(
            target_difficulty=target, available_question_types=list(types)
        ),
    )


def _video(inherited=False, duration=None):
    return VideoItem(
        youtube_id="abc123XYZ01",
        title="Variation of g",
        channel="Jeene",
        hangs_on_node_id="c_height",
        hangs_on_title="g with height",
        inherited=inherited,
        duration_seconds=duration,
    )


def _notes():
    return NotesItem(chapter_id="phy_11_ch8", title="Gravitation notes", page_count=14)


# --- the four catalogues -----------------------------------------------------------


def test_a_rich_chapter_gets_a_full_plan():
    plan = fallback.plan(
        _inventory(
            videos=[_video()],
            notes=[_notes()],
            student=StudentRecord(
                has_history=True,
                scope_attempted=20,
                scope_correct=9,
                scope_accuracy=0.45,
                intent="first_time",
                weak_concepts=[
                    WeakConcept(node_id="c_depth", title="g with depth",
                                attempted=9, wrong=6, accuracy=0.33),
                ],
            ),
        )
    )
    kinds = [s.kind for s in plan.steps]
    assert fallback._TARGET_MIN_STEPS <= len(plan.steps) <= fallback._MAX_STEPS
    assert kinds[-1] == "verify", "a plan always ends in the measurement"
    assert "learn" in kinds, "a rich chapter has something to read or watch"
    assert kinds.count("practise") >= 2


def test_a_questions_only_subtopic_still_gets_a_usable_plan():
    """The common case. Notes exist for eight physics chapters and nowhere else."""
    plan = fallback.plan(_inventory(videos=[], notes=[]))
    assert len(plan.steps) >= fallback._TARGET_MIN_STEPS
    assert plan.steps[-1].kind == "verify"
    assert all(
        item.type == "questions" for step in plan.steps for item in step.items
    ), "with no video and no notes, every step must be questions"


def test_a_student_nobody_knows_anything_about_gets_no_invented_weakness():
    plan = fallback.plan(_inventory(videos=[_video()]))
    assert not any(s.is_foundation for s in plan.steps)
    titles = " ".join(s.title for s in plan.steps)
    assert "not sticking" not in titles, "no weak-spot step without a record"


def test_a_real_prerequisite_gap_opens_the_plan():
    plan = fallback.plan(
        _inventory(
            candidates=[
                FoundationCandidate(
                    node_id="c_newton2",
                    title="Newton's second law",
                    chapter_id="phy_11_ch5",
                    chapter_title="Laws of Motion",
                    source="authored",
                    question_count=22,
                    student_attempted=17,
                    student_accuracy=0.41,
                )
            ],
            student=StudentRecord(has_history=True, intent="first_time"),
        )
    )
    assert plan.steps[0].is_foundation
    assert plan.steps[0].focus_node_ids == ["c_newton2"]
    assert "Laws of Motion" in plan.steps[0].why


# --- foundation is evidence, not decoration ----------------------------------------


@pytest.mark.parametrize(
    "attempted,accuracy,expected",
    [
        (17, 0.41, True),    # a real, well-evidenced gap
        (3, 0.0, False),     # three misses is not evidence
        (20, 0.75, False),   # met it and doing fine
        (0, None, False),    # never touched: unknown, not weak
    ],
)
def test_foundation_needs_the_record_to_say_so(attempted, accuracy, expected):
    """A plan that opens with an earlier chapter every time tells everyone they are behind."""
    plan = fallback.plan(
        _inventory(
            candidates=[
                FoundationCandidate(
                    node_id="c_newton2", title="Newton's second law",
                    source="authored", question_count=22,
                    student_attempted=attempted, student_accuracy=accuracy,
                )
            ]
        )
    )
    assert any(s.is_foundation for s in plan.steps) is expected


def test_foundation_is_skipped_when_there_is_nothing_to_practise_there():
    plan = fallback.plan(
        _inventory(
            candidates=[
                FoundationCandidate(
                    node_id="c_newton2", title="Newton's second law",
                    source="authored", question_count=1,
                    student_attempted=17, student_accuracy=0.41,
                )
            ]
        )
    )
    assert not any(s.is_foundation for s in plan.steps)


# --- steps drop out cleanly ---------------------------------------------------------


def test_no_material_means_no_learn_step_rather_than_an_empty_one():
    plan = fallback.plan(_inventory(videos=[], notes=[]))
    assert not any(s.kind == "learn" for s in plan.steps)


def test_a_scope_with_too_few_pyqs_gets_no_exam_step():
    buckets = [
        QuestionBucket(node_id="c_height", question_type="pyq",
                       difficulty="medium", total=2),
        QuestionBucket(node_id="c_height", question_type="mcq",
                       difficulty="easy", total=20),
    ]
    plan = fallback.plan(_inventory(buckets=buckets, scope_total=22))
    # Checked by shape, not by title: the wording is the teacher's to change.
    pyq_only = [
        item
        for step in plan.steps
        for item in step.items
        if item.selector and item.selector.question_types == ["pyq"]
    ]
    assert pyq_only == []


def test_the_barest_real_scope_gets_two_honest_steps_rather_than_three_padded_ones():
    """A dozen ungraded MCQs, no lecture, no notes, no record.

    Two steps is the right answer here — practise, then check. Padding to a nominal
    minimum would mean inventing work, and a student can tell.
    """
    buckets = [
        QuestionBucket(node_id="c_height", question_type="mcq",
                       difficulty="medium", total=12)
    ]
    plan = fallback.plan(
        _inventory(buckets=buckets, videos=[], notes=[], types=("mcq",), scope_total=12)
    )
    assert [s.kind for s in plan.steps] == ["practise", "verify"]


def test_an_empty_scope_says_so_rather_than_reporting_zero_steps_of_work():
    """Nothing to practise means nothing to plan. JM-4 turns this into a clear refusal."""
    plan = fallback.plan(_inventory(buckets=[], videos=[], notes=[], scope_total=0))
    assert plan.steps == []
    assert "nothing published" in plan.summary
    assert "0 steps" not in plan.summary


def test_a_scope_with_only_ungraded_questions_still_gets_a_checkpoint():
    buckets = [
        QuestionBucket(node_id=c, question_type="mcq", difficulty="unrated", total=8)
        for c in CONCEPTS
    ]
    plan = fallback.plan(_inventory(buckets=buckets, scope_total=24))
    checkpoint = plan.steps[-1]
    assert checkpoint.kind == "verify"
    assert checkpoint.items[0].selector.exclude_seen is True


def test_it_prefers_its_own_video_over_one_inherited_from_the_chapter():
    """A general lecture beats an empty step; a specific one beats the general lecture."""
    inherited = _video(inherited=True)
    inherited.youtube_id = "INHERITED01"
    own = _video()
    own.youtube_id = "OWNVIDEO001"
    # Inherited listed first, so picking [0] blindly would fail this.
    plan = fallback.plan(_inventory(videos=[inherited, own]))
    learn = next(s for s in plan.steps if s.kind == "learn")
    assert learn.items[0].video_id == "OWNVIDEO001"


def test_an_inherited_video_is_still_better_than_no_step_at_all():
    inherited = _video(inherited=True)
    inherited.youtube_id = "INHERITED01"
    plan = fallback.plan(_inventory(videos=[inherited]))
    learn = next(s for s in plan.steps if s.kind == "learn")
    assert learn.items[0].video_id == "INHERITED01"


def test_notes_carry_the_learn_step_when_there_is_no_video():
    plan = fallback.plan(_inventory(videos=[], notes=[_notes()]))
    learn = next(s for s in plan.steps if s.kind == "learn")
    assert learn.items[0].type == "notes"
    assert learn.items[0].notes_chapter_id == "phy_11_ch8"


# --- shape rules --------------------------------------------------------------------


def test_a_plan_never_asks_for_more_questions_than_could_exist():
    """A step that promises ten and opens on six is a bug the student can see."""
    buckets = [
        QuestionBucket(node_id="c_height", question_type="mcq",
                       difficulty="easy", total=4),
    ]
    plan = fallback.plan(_inventory(buckets=buckets, scope_total=4))
    for step in plan.steps:
        for item in step.items:
            if item.selector:
                assert item.selector.count <= 4


def test_the_checkpoint_is_the_only_step_that_excludes_what_was_seen():
    """Meeting a question again is how revision works; measuring on one is not."""
    plan = fallback.plan(_inventory(videos=[_video()], notes=[_notes()]))
    excluding = [
        step.kind
        for step in plan.steps
        for item in step.items
        if item.selector and item.selector.exclude_seen
    ]
    assert excluding == ["verify"]


def test_only_reading_and_watching_can_be_completed_by_tapping():
    """The plan's whole claim is that finishing it means something."""
    plan = fallback.plan(_inventory(videos=[_video()], notes=[_notes()]))
    for step in plan.steps:
        if step.completion.kind == "self":
            assert step.kind in ("learn", "consolidate")
        else:
            assert step.completion.required_questions is not None
            assert step.completion.required_accuracy is not None


def test_every_graded_step_asks_for_no_more_than_it_offers():
    plan = fallback.plan(_inventory(videos=[_video()]))
    for step in plan.steps:
        if step.completion.required_questions is None:
            continue
        offered = sum(i.selector.count for i in step.items if i.selector)
        assert step.completion.required_questions <= offered


def test_the_steps_form_a_straight_line_that_only_ever_looks_backwards():
    plan = fallback.plan(_inventory(videos=[_video()], notes=[_notes()]))
    for index, step in enumerate(plan.steps):
        assert all(d < index for d in step.depends_on), "a step may not depend forwards"
    assert plan.steps[0].depends_on == []


def test_a_plan_is_never_longer_than_anyone_finishes():
    plan = fallback.plan(
        _inventory(
            videos=[_video()],
            notes=[_notes()],
            candidates=[
                FoundationCandidate(
                    node_id="c_newton2", title="Newton's second law",
                    source="authored", question_count=22,
                    student_attempted=17, student_accuracy=0.41,
                )
            ],
            student=StudentRecord(
                has_history=True,
                intent="revising",
                weak_concepts=[
                    WeakConcept(node_id="c_depth", title="g with depth",
                                attempted=9, wrong=6, accuracy=0.33),
                ],
            ),
        )
    )
    assert len(plan.steps) <= fallback._MAX_STEPS
    assert plan.steps[-1].kind == "verify", "trimming must never drop the checkpoint"


def test_the_summary_says_how_long_it_will_take():
    plan = fallback.plan(_inventory(videos=[_video()]))
    assert "steps" in plan.summary
    assert "Acceleration due to gravity" in plan.summary


def test_a_rolled_up_inventory_is_planned_at_subtopic_level():
    """Selectors must use the granularity the counts came from."""
    buckets = [
        QuestionBucket(node_id="phy_11_ch8_g_variation", question_type="mcq",
                       difficulty="medium", total=40)
    ]
    plan = fallback.plan(
        _inventory(
            buckets=buckets,
            scope_total=40,
            rollup=InventoryRollup(applied=True, level="subtopic",
                                   original_bucket_count=400),
        )
    )
    for step in plan.steps:
        for item in step.items:
            if item.selector:
                assert item.selector.concept_node_ids == ["phy_11_ch8_g_variation"]


# --- guidance -----------------------------------------------------------------------


def test_every_step_kind_has_guidance_for_every_intent():
    """The acceptance criterion, checked rather than asserted in a handoff."""
    kinds = set()
    for role, by_intent in GUIDANCE.items():
        for intent in ("first_time", "revising", "exam_soon"):
            lines = by_intent[intent]
            assert 2 <= len(lines) <= 4, f"{role}/{intent} has {len(lines)} lines"
            assert all(line.strip() for line in lines)
        kinds.add(ROLE_KIND[role])
    assert kinds == set(StepKind.__args__)


def test_guidance_falls_back_rather_than_raising_on_an_unknown_intent():
    assert how_to_use("checkpoint", None) == GUIDANCE["checkpoint"]["first_time"]


def test_guidance_gives_instructions_rather_than_encouragement():
    """'Practice makes perfect' tells nobody to do anything."""
    banned = ("you can do it", "practice makes perfect", "believe", "good luck", "!")
    for role, by_intent in GUIDANCE.items():
        for intent, lines in by_intent.items():
            for line in lines:
                lowered = line.lower()
                for phrase in banned:
                    assert phrase not in lowered, f"{role}/{intent}: {line}"


def test_guidance_never_teaches_the_subject():
    """This file is instructions. The moment it explains physics it becomes content."""
    for by_intent in GUIDANCE.values():
        for lines in by_intent.values():
            for line in lines:
                assert "=" not in line
                assert "\\" not in line, "no LaTeX in guidance"


def test_every_step_a_plan_produces_carries_its_instructions():
    plan = fallback.plan(_inventory(videos=[_video()], notes=[_notes()]))
    for step in plan.steps:
        assert 2 <= len(step.how_to_use) <= 4
        assert step.why.strip()


@pytest.mark.parametrize("intent", ["first_time", "revising", "exam_soon"])
def test_intent_changes_the_instructions(intent: Intent):
    plan = fallback.plan(
        _inventory(videos=[_video()], student=StudentRecord(intent=intent))
    )
    checkpoint = plan.steps[-1]
    assert checkpoint.how_to_use == GUIDANCE["checkpoint"][intent]
