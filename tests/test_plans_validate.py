"""Every rung of the validation ladder, against a hand-written bad plan.

Structured outputs guarantee shape. Everything here is about the things they cannot: an
id that is not in the catalogue, a filter nothing can fill, a step that would open on
nothing, prose that reads like teaching. Each test writes the specific plan that breaks
one rule, because a validator is only worth having if you know which failure it catches.

The last test in the file is the one that matters most, and it is the one that found a
real bug: **the deterministic planner's own output must always validate.** If it does not,
either the validator is wrong or the two halves of the feature disagree about what a plan
is — and the first time that happened, the validator was rejecting a perfectly good
foundation step.
"""

import pytest

from app.plans import fallback, validate
from app.plans.schema import (
    FoundationCandidate,
    Inventory,
    Materials,
    NotesItem,
    PlanConstraints,
    PlanItem,
    PlanStep,
    QuestionBucket,
    QuestionSelector,
    ScopeInfo,
    StepCompletion,
    StudentRecord,
    StudyPlanOut,
    TeachingNode,
    VideoItem,
    WeakConcept,
)

CONCEPTS = ["c_height", "c_depth", "c_latitude"]


def _inventory(**kw) -> Inventory:
    base = dict(
        scope=ScopeInfo(
            node_id="sub_1", type="subtopic", title="Acceleration due to gravity",
            chapter_id="ch_8", chapter_title="Gravitation", subject="phy",
        ),
        concepts=[TeachingNode(node_id=c, title=c, parent_id="sub_1") for c in CONCEPTS],
        subtopics=[TeachingNode(node_id="sub_1", title="Acceleration due to gravity")],
        materials=Materials(
            videos=[VideoItem(youtube_id="vid00000001", title="v",
                              hangs_on_node_id="sub_1", hangs_on_title="s")],
            notes=[NotesItem(chapter_id="ch_8", title="notes", page_count=10)],
        ),
        question_buckets=[
            QuestionBucket(node_id=c, question_type=t, difficulty=d, total=8)
            for c in CONCEPTS for t in ("mcq", "pyq") for d in ("easy", "medium", "hard")
        ],
        scope_question_total=144,
        constraints=PlanConstraints(available_question_types=["mcq", "pyq"]),
    )
    base.update(kw)
    return Inventory(**base)


def _selector(**kw) -> QuestionSelector:
    base = dict(concept_node_ids=CONCEPTS, question_types=["mcq"],
                difficulty=["medium"], count=6)
    base.update(kw)
    return QuestionSelector(**base)


def _step(**kw) -> PlanStep:
    base = dict(
        kind="practise", title="Practise", why="because it is next",
        how_to_use=["do six", "check them together"],
        focus_node_ids=CONCEPTS, estimated_minutes=15,
        items=[PlanItem(type="questions", selector=_selector())],
        completion=StepCompletion(kind="accuracy", required_questions=4,
                                  required_accuracy=0.6),
    )
    base.update(kw)
    return PlanStep(**base)


def _checkpoint(**kw) -> PlanStep:
    base = dict(
        kind="verify", title="Check", why="this is what finishing means",
        how_to_use=["one sitting", "no notes"],
        focus_node_ids=CONCEPTS, estimated_minutes=20,
        items=[PlanItem(type="questions",
                        selector=_selector(count=10, exclude_seen=True))],
        completion=StepCompletion(kind="checkpoint", required_questions=8,
                                  required_accuracy=0.7),
    )
    base.update(kw)
    return PlanStep(**base)


def _plan(*steps) -> StudyPlanOut:
    return StudyPlanOut(summary="a plan", steps=list(steps or (_step(), _step(), _checkpoint())))


def _errors(plan, inventory=None):
    return validate.check(plan, inventory or _inventory()).errors


# --- a good plan is accepted -------------------------------------------------------------


def test_a_correct_plan_passes_untouched():
    verdict = validate.check(_plan(), _inventory())
    assert verdict.ok
    assert verdict.repaired == []


# --- the catalogue does not contain that --------------------------------------------------


def test_a_video_that_does_not_exist_is_rejected():
    plan = _plan(
        _step(kind="learn", items=[PlanItem(type="video", video_id="NOPE0000001")],
              completion=StepCompletion(kind="self")),
        _step(), _checkpoint(),
    )
    assert any("not in the catalogue" in e for e in _errors(plan))


def test_notes_for_a_chapter_without_any_are_rejected():
    """The model reached for notes on a *prerequisite* chapter that has none."""
    plan = _plan(
        _step(kind="learn", items=[PlanItem(type="notes", notes_chapter_id="ch_5")],
              completion=StepCompletion(kind="self")),
        _step(), _checkpoint(),
    )
    assert any("do not exist" in e for e in _errors(plan))


def test_a_test_that_does_not_exist_is_rejected():
    plan = _plan(
        _step(items=[PlanItem(type="test", test_id="mock_99")]), _step(), _checkpoint()
    )
    assert any("not in the catalogue" in e for e in _errors(plan))


def test_a_concept_outside_the_scope_is_rejected():
    plan = _plan(_step(focus_node_ids=["c_from_another_chapter"]), _step(), _checkpoint())
    assert any("not in this scope" in e for e in _errors(plan))


def test_a_selector_over_a_concept_outside_the_scope_is_rejected():
    plan = _plan(
        _step(items=[PlanItem(type="questions",
                              selector=_selector(concept_node_ids=["c_elsewhere"]))]),
        _step(), _checkpoint(),
    )
    assert any("not in this scope" in e for e in _errors(plan))


def test_a_selector_naming_a_subtopic_is_fine():
    """`resolve.py` expands any node to its concepts, and the schema says so.

    Comparing selector ids to bucket keys directly rejected subtopic-wide steps as asking
    for questions that do not exist. The plans it rejected were correct.
    """
    plan = _plan(
        _step(items=[PlanItem(type="questions",
                              selector=_selector(concept_node_ids=["sub_1"]))]),
        _step(), _checkpoint(),
    )
    assert _errors(plan) == []


def test_a_selector_naming_the_scope_itself_is_fine():
    plan = _plan(
        _step(items=[PlanItem(type="questions",
                              selector=_selector(concept_node_ids=["sub_1"]))]),
        _step(),
        _checkpoint(items=[PlanItem(
            type="questions",
            selector=_selector(concept_node_ids=["sub_1"], count=10, exclude_seen=True))]),
    )
    assert _errors(plan) == []


def test_groundwork_counts_even_though_it_has_no_buckets():
    """Foundation sits outside the scope, so it has only a count on the candidate.

    Without this the validator rejected every foundation step ever written, including the
    deterministic planner's own.
    """
    inventory = _inventory(
        foundation_candidates=[FoundationCandidate(
            node_id="c_newton2", title="Newton's second law", source="authored",
            question_count=20, student_attempted=9, student_accuracy=0.3,
        )]
    )
    plan = _plan(
        _step(is_foundation=True, focus_node_ids=["c_newton2"],
              items=[PlanItem(type="questions",
                              selector=_selector(concept_node_ids=["c_newton2"], count=5))]),
        _step(), _checkpoint(),
    )
    assert _errors(plan, inventory) == []


def test_groundwork_on_something_that_was_not_offered_is_rejected():
    plan = _plan(
        _step(is_foundation=True, focus_node_ids=["c_depth"]), _step(), _checkpoint()
    )
    assert any("not in foundation_candidates" in e for e in _errors(plan))


# --- a student would see something wrong ---------------------------------------------------


def test_a_step_with_nothing_to_open_is_rejected():
    plan = _plan(_step(items=[]), _step(), _checkpoint())
    assert any("no material to open" in e for e in _errors(plan))


def test_a_plan_that_does_not_end_in_a_measurement_is_rejected():
    assert any("must be a `verify` step" in e for e in _errors(_plan(_step(), _step(), _step())))


def test_a_checkpoint_anywhere_but_last_is_rejected():
    assert any("Only the final step" in e
               for e in _errors(_plan(_checkpoint(), _step(), _checkpoint())))


def test_a_checkpoint_resting_only_on_ungraded_questions_is_rejected():
    """It claims to measure at a difficulty. An ungraded question cannot support that."""
    plan = _plan(_step(), _step(), _checkpoint(items=[PlanItem(
        type="questions",
        selector=_selector(difficulty=["unrated"], count=10, exclude_seen=True))]))
    assert any("ungraded" in e for e in _errors(plan))


@pytest.mark.parametrize(
    "text",
    [
        "The acceleration is 9.8 m/s at the surface.",
        r"Remember \frac{GM}{r^2} before you start.",
        "For each one, the answer is in the second option.",
        "Note that g = 9.8 and work from there.",
    ],
)
def test_prose_that_teaches_is_rejected(text):
    """A planner writing physics is writing invented physics — it has seen no content."""
    plan = _plan(_step(how_to_use=[text, "then check"]), _step(), _checkpoint())
    assert any("Guidance says what to do" in e for e in _errors(plan))


@pytest.mark.parametrize(
    "text",
    ["Open the node and read it.", "Work through this bucket of questions.",
     "Pick anything from the catalogue."],
)
def test_words_from_the_data_model_are_rejected(text):
    """A student has never heard of a node. This leaked into a real generated plan."""
    plan = _plan(_step(how_to_use=[text, "then check"]), _step(), _checkpoint())
    assert any("from the catalogue, not from studying" in e for e in _errors(plan))


def test_too_few_or_too_many_instructions_is_rejected():
    assert any("two to four" in e
               for e in _errors(_plan(_step(how_to_use=["one"]), _step(), _checkpoint())))
    assert any("two to four" in e for e in _errors(
        _plan(_step(how_to_use=["a", "b", "c", "d", "e"]), _step(), _checkpoint())))


def test_a_plan_longer_than_anyone_finishes_is_rejected():
    plan = _plan(*([_step()] * 8), _checkpoint())
    assert any("at most" in e for e in _errors(plan))


def test_an_empty_plan_is_rejected():
    assert any("no steps" in e for e in _errors(StudyPlanOut(summary="", steps=[])))


# --- repaired rather than rejected -----------------------------------------------------------


def test_a_count_larger_than_the_catalogue_is_clamped_not_rejected():
    plan = _plan(
        _step(items=[PlanItem(type="questions", selector=_selector(count=999))]),
        _step(), _checkpoint(),
    )
    verdict = validate.check(plan, _inventory())
    assert verdict.ok
    assert plan.steps[0].items[0].selector.count == 24  # 3 concepts x one bucket of 8
    assert any("reduced" in r for r in verdict.repaired)


def test_an_unfillable_filter_is_dropped_when_the_step_keeps_another():
    """Rejecting a six-step plan over one thin sub-filter spends a round trip to fix
    something the validator can correct exactly."""
    plan = _plan(
        _step(items=[
            PlanItem(type="questions", selector=_selector()),
            PlanItem(type="questions",
                     selector=_selector(question_types=["ncert_exemplar"], count=4)),
        ]),
        _step(), _checkpoint(),
    )
    verdict = validate.check(plan, _inventory())
    assert verdict.ok
    assert len(plan.steps[0].items) == 1
    assert any("dropped a filter" in r for r in verdict.repaired)


def test_an_unfillable_filter_that_is_the_only_one_is_rejected():
    plan = _plan(
        _step(items=[PlanItem(type="questions",
                              selector=_selector(question_types=["ncert_exemplar"]))]),
        _step(), _checkpoint(),
    )
    assert any("do not exist" in e for e in _errors(plan))


def test_a_graded_step_marked_self_is_coerced_the_strict_way():
    """Never the other way round: making the plan tappable is the one unsurvivable bug."""
    plan = _plan(_step(completion=StepCompletion(kind="self")), _step(), _checkpoint())
    verdict = validate.check(plan, _inventory())
    assert verdict.ok
    assert plan.steps[0].completion.kind == "accuracy"
    assert plan.steps[0].completion.required_questions is not None


def test_a_learn_step_with_only_questions_is_relabelled():
    """It reads to a student as a lesson and opens as a quiz."""
    plan = _plan(_step(kind="learn"), _step(), _checkpoint())
    verdict = validate.check(plan, _inventory())
    assert verdict.ok
    assert plan.steps[0].kind == "practise"
    assert any("relabelled" in r for r in verdict.repaired)


def test_a_learn_step_with_something_to_watch_keeps_its_name():
    plan = _plan(
        _step(kind="learn", items=[PlanItem(type="video", video_id="vid00000001")],
              completion=StepCompletion(kind="self")),
        _step(), _checkpoint(),
    )
    verdict = validate.check(plan, _inventory())
    assert verdict.ok
    assert plan.steps[0].kind == "learn"


def test_too_much_groundwork_is_demoted_not_deleted():
    """Deleting would shorten the plan below its floor and cascade into a second failure."""
    inventory = _inventory(foundation_candidates=[
        FoundationCandidate(node_id=f"c_pre{i}", title="p", source="authored",
                            question_count=20)
        for i in range(3)
    ])
    plan = _plan(*[
        _step(is_foundation=True, focus_node_ids=[f"c_pre{i}"],
              items=[PlanItem(type="questions",
                              selector=_selector(concept_node_ids=[f"c_pre{i}"], count=5))])
        for i in range(3)
    ], _checkpoint())
    verdict = validate.check(plan, inventory)
    assert verdict.ok
    assert sum(1 for s in plan.steps if s.is_foundation) == 2
    assert len(plan.steps) == 4, "demoted, not removed"


def test_a_checkpoint_that_forgot_to_exclude_seen_questions_is_corrected():
    plan = _plan(_step(), _step(), _checkpoint(items=[PlanItem(
        type="questions", selector=_selector(count=10, exclude_seen=False))]))
    verdict = validate.check(plan, _inventory())
    assert verdict.ok
    assert plan.steps[-1].items[0].selector.exclude_seen is True


def test_a_dependency_on_a_later_step_is_dropped():
    plan = _plan(_step(depends_on=[2]), _step(), _checkpoint())
    verdict = validate.check(plan, _inventory())
    assert verdict.ok
    assert plan.steps[0].depends_on == []


def test_an_accuracy_bar_outside_the_sane_range_is_clamped():
    plan = _plan(
        _step(completion=StepCompletion(kind="accuracy", required_questions=4,
                                        required_accuracy=0.99)),
        _step(), _checkpoint(),
    )
    verdict = validate.check(plan, _inventory())
    assert verdict.ok
    assert plan.steps[0].completion.required_accuracy == 0.9


# --- the invariant that tied the two halves together -------------------------------------------


@pytest.mark.parametrize("has_video", [True, False])
@pytest.mark.parametrize("has_notes", [True, False])
@pytest.mark.parametrize("has_record", [True, False])
@pytest.mark.parametrize("proficiency", ["basic", "intermediate", "advanced"])
def test_the_deterministic_planner_always_produces_a_valid_plan(
    has_video, has_notes, has_record, proficiency
):
    """If this ever fails, the two halves of the feature disagree about what a plan is.

    It did fail, the first time it was written: the validator rejected the deterministic
    planner's own foundation step, because groundwork sits outside the scope and has no
    buckets. That would have meant every model plan containing a foundation step was
    rejected too — and the fallback rate said the model was bad when the validator was.
    """
    from app.plans.inventory import _TARGET_DIFFICULTY

    inventory = _inventory(
        materials=Materials(
            videos=[VideoItem(youtube_id="vid00000001", title="v",
                              hangs_on_node_id="sub_1", hangs_on_title="s")]
            if has_video else [],
            notes=[NotesItem(chapter_id="ch_8", title="n", page_count=10)]
            if has_notes else [],
        ),
        foundation_candidates=[FoundationCandidate(
            node_id="c_newton2", title="Newton's second law", source="authored",
            question_count=20,
            student_attempted=9 if has_record else 0,
            student_accuracy=0.3 if has_record else None,
        )],
        student=StudentRecord(
            has_history=has_record,
            intent="first_time",
            weak_concepts=[WeakConcept(node_id="c_depth", title="d", attempted=9,
                                       wrong=6, accuracy=0.33)] if has_record else [],
        ),
        constraints=PlanConstraints(
            target_difficulty=_TARGET_DIFFICULTY[proficiency],
            available_question_types=["mcq", "pyq"],
        ),
    )
    verdict = validate.check(fallback.plan(inventory), inventory)
    assert verdict.ok, verdict.errors
