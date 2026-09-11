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
            "True only if the material you were given settles the question. False if it "
            "does not, if the question is about something other than this chapter, or "
            "if you are declining for any other reason — and then say why in `answer`."
        )
    )
    used_concept_ids: list[str] = Field(
        description=(
            "The ids in square brackets of the concepts you actually drew on, copied "
            "exactly from the material. Empty if none. Never an id you were not given."
        )
    )
    used_question_ids: list[str] = Field(
        description=(
            "The ids in square brackets of the questions or worked examples you actually "
            "drew on, copied exactly from the material. Empty if none."
        )
    )
    used_notes: bool = Field(
        description="True if you drew on the chapter notes you were given."
    )
