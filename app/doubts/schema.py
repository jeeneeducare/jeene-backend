"""The shape an answer has to arrive in.

The field descriptions are not documentation — they are sent to the provider as part of
the JSON schema and are the last thing the model reads about each field. Wording them
carelessly is the same class of mistake as wording the prompt carelessly.

Structured output guarantees the shape and nothing else. Whether the ids are real is
`answer.py`'s job, exactly as `plans/validate.py` is the planner's: a schema can say "a
list of strings" and cannot say "strings I actually gave you".
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class DoubtAnswer(BaseModel):
    """One reply to one doubt."""

    answer: str = Field(
        description=(
            "The reply to show the student. Prose, with mathematics inline between "
            "single dollar signs. Leads with the answer, then teaches the idea behind "
            "it. Three or four short paragraphs at most."
        )
    )
    answered: bool = Field(
        description=(
            "True if you were able to answer the question from the material, even "
            "partly. Partly counts: if the material explains most of it, answer that "
            "much, say in `answer` which part you could not cover, and still set this "
            "true. False only when the material let you answer none of it — the "
            "question is about another chapter or another subject, or is not a doubt "
            "about what they are studying, or you are declining for safety."
        )
    )
    used_concept_ids: list[str] = Field(
        description=(
            "The short references — C1, C7 — of the concepts you actually drew on, "
            "without their brackets. Empty if none. Only references the material shows."
        )
    )
    used_question_ids: list[str] = Field(
        description=(
            "The short references — Q1, Q3 — of the questions or worked examples you "
            "actually drew on, without their brackets. Empty if none."
        )
    )
    used_notes: bool = Field(
        description="True if you drew on the chapter notes you were given."
    )
