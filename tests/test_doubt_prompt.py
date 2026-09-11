"""How the material is laid out for the model to quote from.

Mostly one question: is everything the model needs present, and is everything it must not
reveal actually absent? A rule in the prompt is a request; leaving something out of the
block is a guarantee, and the two are not close to equivalent.
"""

from __future__ import annotations

import re

from app.doubts.context import Anchor, Attempt, Concept, Material, Solution
from app.doubts.prompt import (
    MAX_HISTORY_CHARS,
    MAX_HISTORY_TURNS,
    SYSTEM,
    history_text,
    material_text,
)

ANCHOR = Anchor(
    kind="node", anchor_id="ch1", chapter_id="ch1", chapter_title="Gravitation",
    scope_title="Gravitation", scope_node_id="ch1",
)

WORKED = Solution(
    question_id="q_worked",
    stem="Which of these is the escape speed of the Earth?",
    options="(a) 11.2 km/s  (b) 7.9 km/s",
    correct="a",
    explanation="Substituting into $v=\\sqrt{2GM/R}$ gives about 11.2 km/s.",
)


def a_material(**over) -> Material:
    base = dict(
        anchor=ANCHOR,
        concepts=[Concept("c_escape", "Escape Speed", "The least speed to leave.")],
        solutions=[WORKED],
        notes="",
        recent=[],
        focus=None,
    )
    return Material(**{**base, **over})


# --- what is there --------------------------------------------------------------------


def test_every_item_carries_the_id_the_model_has_to_copy_back():
    """`used_concept_ids` is checked against what was sent, so an id the model had to
    reconstruct from a title is an id it will get wrong — and a correct answer would then
    be thrown away for citing something it was looking straight at."""
    text = material_text(a_material())

    assert "[c_escape]" in text
    assert "[q_worked]" in text


def test_the_chapter_and_what_they_are_looking_at_are_both_named():
    text = material_text(a_material())

    assert "Gravitation" in text
    assert "THE STUDENT IS LOOKING AT" in text


def test_the_notes_go_in_whole_when_there_are_any():
    text = material_text(a_material(notes="Gravitation is the attraction between masses."))

    assert "CHAPTER NOTES" in text
    assert "attraction between masses" in text


def test_a_chapter_with_no_notes_has_no_notes_section():
    """An empty heading invites the model to say the notes are missing, which is a thing
    the student can do nothing about and did not ask."""
    assert "CHAPTER NOTES" not in material_text(a_material(notes=""))


def test_what_the_student_got_wrong_is_said_plainly():
    text = material_text(a_material(recent=[
        Attempt("q_worked", "Which of these is the escape speed?", was_correct=False),
    ]))

    assert "got it wrong" in text


# --- what is deliberately not -----------------------------------------------------------


def test_a_worked_examples_answer_is_never_sent_at_all():
    """The student is about to practise these very questions.

    The prompt also forbids revealing them, but a rule is a request and an omission is a
    guarantee: what is never sent cannot be leaked by a model having an off day, or by
    someone talking it into one.
    """
    text = material_text(a_material())

    assert "Correct answer" not in text
    assert WORKED.explanation in text, "the explanation is the whole teaching value"


def test_the_question_they_actually_asked_about_keeps_its_answer():
    """The exception, and the reason the feature exists: explaining the one on screen is
    the point, and doing it without the answer would be a riddle."""
    text = material_text(a_material(focus=WORKED, solutions=[]))

    assert "Correct answer: a" in text
    assert "explain this one fully" in text


def test_the_focus_answer_does_not_leak_into_the_worked_examples():
    """Both sections exist in the same block. The one that may show an answer must not
    turn into permission for the other."""
    other = Solution("q_other", "Another one", "(a) yes", "b", "Because of gravity.")
    text = material_text(a_material(focus=WORKED, solutions=[other]))

    assert text.count("Correct answer") == 1
    assert "[q_other]" in text


# --- the prompt itself -------------------------------------------------------------------


def test_the_system_prompt_is_a_constant_not_a_template():
    """It is the half the provider caches, and it holds only while every byte is the same
    on every call. A `{placeholder}` in here means somebody meant to format it — which
    would work, and would quietly take the cache with it.

    Braces alone prove nothing: the prompt teaches `\\sqrt{2GM/R}`. What is looked for is
    a brace around a plain lowercase name, which is what a substitution looks like and
    what LaTeX in this prompt never does.
    """
    assert re.search(r"\{[a-z_][a-z0-9_]*\}", SYSTEM) is None
    assert "%s" not in SYSTEM


def test_the_prompt_says_which_maths_the_app_can_actually_draw():
    """The app splits on single `$` and rasterises each span; `$$` and `\\begin{}` come
    out as raw backslashes in front of the student. This is the rule most likely to be
    lost in a future edit, so it is pinned here."""
    assert "$$" in SYSTEM and "Never" in SYSTEM


# --- the conversation so far ---------------------------------------------------------------


def test_each_turn_says_who_said_it():
    text = history_text([("student", "what is escape speed"), ("jeene", "It is...")])

    assert text.startswith("Student: what is escape speed")
    assert "You: It is..." in text


def test_only_the_last_few_turns_go_back():
    text = history_text([("student", f"turn {i}") for i in range(20)])

    assert "turn 19" in text
    assert "turn 0" not in text
    assert len(text.splitlines()) == MAX_HISTORY_TURNS


def test_one_enormous_turn_cannot_swallow_the_material():
    text = history_text([("student", "x" * (MAX_HISTORY_CHARS * 3))])

    assert len(text) < MAX_HISTORY_CHARS + 100
    assert text.endswith("…")
