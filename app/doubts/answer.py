"""Ask the model, then check that what came back was built from what was sent.

The check is the point of this module. Structured output guarantees a well-formed reply
and says nothing about whether it is grounded, and "grounded" is the only property that
makes this feature safe to put in front of a sixteen-year-old: the app has one voice, an
answer appears beside notes a teacher wrote, and a paragraph the model remembered from
somewhere else is indistinguishable from one the chapter actually supports.

So every id the model reports using is checked against what it was given. An id that was
never sent is not a formatting slip — it is the model telling you, in the one field it
cannot fake, that it went outside its material. That answer is dropped.

Dropped rather than retried. A student is watching a spinner, a second call doubles the
wait for an answer that is usually no better, and "I could not answer this one from your
chapter" is a perfectly good thing to say to a student. The planner can retry because
nobody is waiting on a repair round; this cannot.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from app.doubts import prompt as prompt_module
from app.doubts.context import Material
from app.doubts.schema import DoubtAnswer
from app.providers.base import ProviderUsage

logger = logging.getLogger(__name__)

#: Room for four short paragraphs with some mathematics in them — and, on a reasoning
#: model, for the thinking that precedes them, because reasoning tokens count against this
#: same ceiling. Measured on gpt-5: a three-sentence refusal spent ~900 tokens, almost all
#: of it reasoning, and 1,500 truncated a real answer into an unparseable reply.
MAX_ANSWER_TOKENS = 6_000

#: Explaining a chapter's own material back to a student is reading, not reasoning. The
#: deep-thought default spends more time and more money to say the same thing, and a
#: student is watching a spinner while it does.
REASONING_EFFORT = "low"

#: What the student sees when we drop an answer. Deliberately not an error: from where
#: they are sitting, "this chapter does not cover it" is true and actionable, and an
#: apology about an internal check is neither.
COULD_NOT_ANSWER = (
    "I could not answer that one from this chapter's material. Try asking it a "
    "different way, or check with your teacher."
)

#: Why we dropped it, for the log and the metric. Never shown to the student.
UNGROUNDED = "cited material it was not given"
EMPTY = "returned nothing to show"


@dataclass(frozen=True)
class Outcome:
    """What to store and what to show, after the answer has been checked."""

    text: str
    answered: bool
    used_concept_ids: list[str] = field(default_factory=list)
    used_question_ids: list[str] = field(default_factory=list)
    used_notes: bool = False
    usage: ProviderUsage | None = None
    prompt_version: int = prompt_module.PROMPT_VERSION
    #: Set only when *we* rejected the model's reply, as opposed to the model declining.
    #: A rising count here is the signal that the prompt or the material has drifted.
    dropped_because: str | None = None


async def answer(
    provider,
    material: Material,
    question: str,
    history: list[tuple[str, str]] | None = None,
) -> Outcome:
    """Answer one doubt from one chapter's material, or decline to.

    Raises `ProviderError` if the call itself fails — that is the caller's to turn into a
    "try again in a moment", and is a different thing from an answer that came back and
    was not good enough.
    """
    messages_context = prompt_module.material_text(material)
    if history:
        messages_context += (
            "\n\nEARLIER IN THIS CONVERSATION:\n" + prompt_module.history_text(history)
        )

    parsed, usage = await provider.read_json(
        system_prompt=prompt_module.SYSTEM,
        context=messages_context,
        user_text=question,
        schema=DoubtAnswer,
        max_output_tokens=MAX_ANSWER_TOKENS,
    )
    assert isinstance(parsed, DoubtAnswer)

    text = parsed.answer.strip()
    if not text:
        logger.warning("doubt answer dropped reason=%s", EMPTY)
        return Outcome(
            text=COULD_NOT_ANSWER, answered=False, usage=usage, dropped_because=EMPTY
        )

    sorted_ids = _sort_citations(parsed, material)
    if sorted_ids is None:
        everything = material.concept_ids() | material.question_ids()
        # Log the ids, never the answer: an id is safe to keep and the answer text is a
        # student's conversation. Which ids were invented is what you need to tell a
        # drifting prompt from a material block that changed shape.
        logger.warning(
            "doubt answer dropped reason=%s chapter=%s bad_concepts=%s bad_questions=%s",
            UNGROUNDED,
            material.anchor.chapter_id,
            sorted(_unknown(parsed.used_concept_ids, everything)),
            sorted(_unknown(parsed.used_question_ids, everything)),
        )
        return Outcome(
            text=COULD_NOT_ANSWER,
            answered=False,
            usage=usage,
            dropped_because=UNGROUNDED,
        )
    concepts, questions = sorted_ids

    return Outcome(
        text=text,
        answered=parsed.answered,
        used_concept_ids=concepts,
        used_question_ids=questions,
        # Claiming the notes when there were none is the same failure as an invented id,
        # but it is not worth dropping an otherwise grounded answer over: the notes are
        # either there or they are not, and the record should say what was true.
        used_notes=parsed.used_notes and bool(material.notes),
        usage=usage,
    )


def _clean(reported_id: str) -> str:
    """One reported id, without the punctuation the material block wraps it in."""
    return reported_id.strip().strip("[]").strip()


def _unknown(reported: list[str], allowed: set[str]) -> set[str]:
    """The ids that are genuinely not in the material — for the log, not the student.

    Checked against everything we sent rather than against one field, so a misfiled id
    never appears in the log as an invented one.
    """
    return {_clean(r) for r in reported} - allowed


def _sort_citations(
    parsed: DoubtAnswer, material: Material
) -> tuple[list[str], list[str]] | None:
    """The cited ids, filed correctly and deduplicated — or None to bin the answer.

    The property being defended is narrow and worth stating exactly: *every id cited was
    an id we sent*. An id we never sent means the model was working from something other
    than the material, and since there is no way to tell which sentences came from where,
    the whole answer goes. Nothing less than that is safe to show a student.

    Two things that look like violations and are not, both found on the first real runs:

    - **Brackets.** The material block writes `[c_escape] Escape Speed — …`, so ids came
      back wearing the brackets. The model copied what it was shown, as asked, and a good
      answer was thrown away for punctuation.
    - **The wrong field.** A question id turned up under `used_concept_ids`. It was still
      an id we sent; the model just filed it wrong. Moving it is right — dropping the
      answer would be punishing a clerical slip with the penalty meant for invention.

    Tolerating both costs nothing, because neither weakens the actual check.
    """
    concepts: list[str] = []
    questions: list[str] = []
    allowed_concepts = material.concept_ids()
    allowed_questions = material.question_ids()

    for reported_id in list(parsed.used_concept_ids) + list(parsed.used_question_ids):
        node_id = _clean(reported_id)
        if node_id in allowed_concepts:
            bucket = concepts
        elif node_id in allowed_questions:
            bucket = questions
        else:
            return None
        if node_id not in bucket:
            bucket.append(node_id)

    return concepts, questions
