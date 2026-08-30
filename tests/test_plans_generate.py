"""The ladder: generate, repair once, fall back — and never raise.

Driven by a fake provider throughout. No test in this file touches the network, which is
the point: the behaviour worth guaranteeing is what happens when the provider is slow,
absent, broken or wrong, and none of those are easy to arrange with a real one.

The property that matters more than any individual case: **every path returns a plan.**
There is no arrangement of outage, malformed output, missing key or disabled flag that
produces an error, because this call sits on the path a student waits on.
"""

import asyncio
import inspect
import logging

import pytest

from app.plans import prompt
from app.plans.generate import generate
from app.plans.schema import (
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
    StudyPlanOut,
    TeachingNode,
    VideoItem,
)
from app.providers.base import PlannerProvider, ProviderError, ProviderUsage

CONCEPTS = ["c_height", "c_depth"]


def _inventory() -> Inventory:
    return Inventory(
        scope=ScopeInfo(node_id="sub_1", type="subtopic", title="A subtopic",
                        chapter_id="ch_8", chapter_title="Gravitation"),
        concepts=[TeachingNode(node_id=c, title=c, parent_id="sub_1") for c in CONCEPTS],
        subtopics=[TeachingNode(node_id="sub_1", title="A subtopic")],
        materials=Materials(
            videos=[VideoItem(youtube_id="vid00000001", title="v",
                              hangs_on_node_id="sub_1", hangs_on_title="s")],
            notes=[NotesItem(chapter_id="ch_8", title="n", page_count=8)],
        ),
        question_buckets=[
            QuestionBucket(node_id=c, question_type="mcq", difficulty=d, total=10)
            for c in CONCEPTS for d in ("easy", "medium", "hard")
        ],
        scope_question_total=60,
        constraints=PlanConstraints(available_question_types=["mcq"]),
    )


def _good_plan() -> StudyPlanOut:
    def step(kind, completion, **kw):
        base = dict(
            kind=kind, title="A step", why="because",
            how_to_use=["do six", "check them together"],
            focus_node_ids=CONCEPTS, estimated_minutes=15,
            items=[PlanItem(type="questions", selector=QuestionSelector(
                concept_node_ids=CONCEPTS, question_types=["mcq"],
                difficulty=["medium"], count=6))],
            completion=completion,
        )
        base.update(kw)
        return PlanStep(**base)

    return StudyPlanOut(summary="fine", steps=[
        step("practise", StepCompletion(kind="accuracy", required_questions=4,
                                        required_accuracy=0.6)),
        step("practise", StepCompletion(kind="accuracy", required_questions=4,
                                        required_accuracy=0.6)),
        step("verify", StepCompletion(kind="checkpoint", required_questions=8,
                                      required_accuracy=0.7),
             items=[PlanItem(type="questions", selector=QuestionSelector(
                 concept_node_ids=CONCEPTS, question_types=["mcq"],
                 difficulty=["medium"], count=10, exclude_seen=True))]),
    ])


def _bad_plan() -> StudyPlanOut:
    """Names a video that is not in the catalogue. Fatal, not repairable."""
    plan = _good_plan()
    plan.steps[0].items = [PlanItem(type="video", video_id="NOTREAL0001")]
    plan.steps[0].completion = StepCompletion(kind="self")
    plan.steps[0].kind = "learn"
    return plan


class _FakeProvider:
    """Answers with whatever it was handed, in order, and records what it was asked."""

    name = "fake"
    model = "fake-1"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def read_json(self, system_prompt, context, user_text, schema):
        # The port covers both jobs the real provider does; this fake exists for the
        # planning half. Raising rather than returning a stub answer, so a generation
        # test that somehow reaches the reading path says so instead of passing quietly.
        raise AssertionError("the planning fake was asked to read a message")

    async def generate_plan(self, system_prompt, inventory_json, schema,
                            repair_errors=None):
        self.calls.append({
            "system": system_prompt,
            "inventory": inventory_json,
            "repair_errors": repair_errors,
        })
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer, ProviderUsage(provider=self.name, model=self.model,
                                     input_tokens=100, output_tokens=50, latency_ms=10)


def _run(provider, enabled=True):
    return asyncio.run(generate(_inventory(), provider, enabled=enabled))


# --- the three rungs ------------------------------------------------------------------


def test_a_good_plan_on_the_first_call_is_used():
    provider = _FakeProvider(_good_plan())
    result = _run(provider)
    assert result.origin == "model"
    assert result.provider == "fake"
    assert result.model == "fake-1"
    assert len(provider.calls) == 1
    assert provider.calls[0]["repair_errors"] is None


def test_a_bad_plan_is_repaired_rather_than_retried():
    """An identical request would most likely produce an identical answer."""
    provider = _FakeProvider(_bad_plan(), _good_plan())
    result = _run(provider)
    assert result.origin == "model"
    assert len(provider.calls) == 2
    errors = provider.calls[1]["repair_errors"]
    assert errors, "the second call must carry the specific failures"
    assert any("NOTREAL0001" in e for e in errors)


def test_two_bad_plans_fall_back_rather_than_trying_a_third_time():
    provider = _FakeProvider(_bad_plan(), _bad_plan())
    result = _run(provider)
    assert result.origin == "fallback"
    assert result.plan.steps, "the fallback must produce a real plan"
    assert len(provider.calls) == 2, "two is the ceiling"


def test_a_provider_that_fails_falls_back_immediately():
    provider = _FakeProvider(ProviderError("timeout"))
    result = _run(provider)
    assert result.origin == "fallback"
    assert len(provider.calls) == 1, "no point repairing what never answered"


def test_a_provider_that_fails_on_the_repair_still_falls_back():
    provider = _FakeProvider(_bad_plan(), ProviderError("timeout"))
    result = _run(provider)
    assert result.origin == "fallback"


def test_no_provider_means_the_deterministic_planner_with_no_call():
    result = _run(None)
    assert result.origin == "fallback"
    assert result.plan.steps


def test_the_flag_being_off_means_no_call_is_made():
    provider = _FakeProvider(_good_plan())
    result = _run(provider, enabled=False)
    assert result.origin == "fallback"
    assert provider.calls == [], "disabled must mean not called, not called-and-ignored"


@pytest.mark.parametrize("provider_factory", [
    lambda: None,
    lambda: _FakeProvider(ProviderError("x")),
    lambda: _FakeProvider(_bad_plan(), _bad_plan()),
    lambda: _FakeProvider(_good_plan()),
])
def test_every_path_returns_a_plan(provider_factory):
    """This call sits on the path a student is watching a spinner on."""
    result = _run(provider_factory())
    assert result.plan.steps
    assert result.origin in ("model", "fallback")
    assert result.prompt_version == prompt.PROMPT_VERSION


# --- what the provider is given ---------------------------------------------------------


def test_the_provider_gets_the_frozen_prompt_and_the_serialised_inventory():
    provider = _FakeProvider(_good_plan())
    _run(provider)
    call = provider.calls[0]
    assert call["system"] == prompt.SYSTEM
    assert '"scope"' in call["inventory"]


def test_the_system_prompt_never_varies():
    """Providers cache long identical prefixes; one varying byte destroys that."""
    # Not an f-string, and not built at import time from anything that varies.
    source = inspect.getsource(prompt)
    assert 'SYSTEM = """' in source, "the prompt must be a plain literal"
    assert ".format(" not in source and "f\"\"\"" not in source
    provider_a, provider_b = _FakeProvider(_good_plan()), _FakeProvider(_good_plan())
    _run(provider_a)
    _run(provider_b)
    assert provider_a.calls[0]["system"] == provider_b.calls[0]["system"]


def test_the_prompt_carries_a_version_that_is_stamped_on_the_plan():
    assert isinstance(prompt.PROMPT_VERSION, int)
    assert _run(_FakeProvider(_good_plan())).prompt_version == prompt.PROMPT_VERSION


def test_the_repair_instruction_names_the_failures_and_asks_for_nothing_else():
    text = prompt.repair_instruction(["a video does not exist", "a concept is out of scope"])
    assert "a video does not exist" in text
    assert "a concept is out of scope" in text
    assert "Change only what these require" in text


# --- what is logged ----------------------------------------------------------------------


def test_nothing_the_provider_saw_or_said_reaches_the_log(caplog):
    """A log that accumulated the inventory would be a second copy of exactly what the
    boundary refuses to send, somewhere with weaker access control than the database."""
    caplog.set_level(logging.INFO)
    _run(_FakeProvider(_bad_plan(), _good_plan()))
    logged = "\n".join(r.getMessage() for r in caplog.records)

    assert "A subtopic" not in logged, "no scope title"
    assert "how_to_use" not in logged and "summary" not in logged
    assert "You are a study planner" not in logged, "no prompt"
    assert "sub_1" in logged, "the scope id is the one identifier worth logging"
    assert "model=fake-1" in logged
    assert "in=100" in logged and "out=50" in logged and "ms=" in logged


def test_the_fallback_says_why_it_ran():
    """'Why did this student get a thin plan' has to be answerable without guessing."""
    caplog_messages = []
    handler = logging.Handler()
    handler.emit = lambda record: caplog_messages.append(record.getMessage())
    logger = logging.getLogger("app.plans.generate")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        _run(None)
        _run(_FakeProvider(ProviderError("x")))
        _run(_FakeProvider(_bad_plan(), _bad_plan()))
    finally:
        logger.removeHandler(handler)
    reasons = " ".join(caplog_messages)
    assert "no provider" in reasons
    assert "provider error" in reasons
    assert "rejected twice" in reasons


# --- the protocol ---------------------------------------------------------------------------


def test_the_fake_satisfies_the_same_protocol_as_the_real_one():
    assert isinstance(_FakeProvider(), PlannerProvider)


def test_a_missing_key_or_model_builds_no_provider_rather_than_failing():
    """A fresh instance without its environment set is a normal Tuesday."""
    from app.providers.openai_provider import build_provider

    assert build_provider(None, "gpt-x") is None
    assert build_provider("key", None) is None
    assert build_provider("", "") is None
    assert build_provider("key", "gpt-x") is not None
