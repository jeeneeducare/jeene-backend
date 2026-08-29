"""What to actually do with the material, per kind of step and per intent.

This is the part of a plan that makes it a plan. "Practise ten PYQs on gravitation" is a
link with extra words; "do the first three without looking at anything, then check all
three at once" is a study technique. The whole feature turns on the second kind of
sentence, so they are authored here rather than improvised, and the model in JM-5 is
shown these as the standard to write to.

Keyed by *role* rather than by step kind, because several roles share a kind and want
quite different instructions: a first-contact practice set and an exam-shape practice set
are both `practise` and nothing about how you use them is the same.

Rules these were written to, and worth keeping to when editing:

  * Instructions, not encouragement. "Practice makes perfect" tells nobody to do anything.
  * Nothing that names a fact about the subject. This file is guidance, not teaching, and
    the moment it starts explaining physics it has to be reviewed like content.
  * Nothing that assumes a duration, a page number or a question's contents — none of
    which the planner reliably has.
  * Two to four lines. A step with six instructions is a step nobody reads.

**Status: written by the developer, awaiting the teacher's read.** The acceptance
criterion for this ticket asks for a teacher review, and that has not happened yet — the
strings are here so it can. See the handoff.
"""

from __future__ import annotations

from typing import Literal

from app.plans.schema import Intent, StepKind

# One role per distinct job a step can do. `kind` is what the schema and the app see;
# the role is how this file and `fallback.py` talk about it.
Role = Literal[
    "foundation",
    "orient",
    "first_contact",
    "weak_spots",
    "exam_shape",
    "consolidate",
    "checkpoint",
    "remediation",
]

ROLE_KIND: dict[Role, StepKind] = {
    "foundation": "practise",
    "orient": "learn",
    "first_contact": "practise",
    "weak_spots": "practise",
    "exam_shape": "practise",
    "consolidate": "consolidate",
    "checkpoint": "verify",
    "remediation": "practise",
}

_DEFAULT_INTENT: Intent = "first_time"

GUIDANCE: dict[Role, dict[Intent, list[str]]] = {
    # Added after a missed checkpoint, on the concepts the check itself found. The
    # difference from `weak_spots` is what the student has just been through: they have
    # an answer sheet in front of them and a specific disappointment, so this says what
    # to do with both rather than starting from "your record suggests".
    "remediation": {
        "first_time": [
            "These are the ideas the check found, not the whole topic again.",
            "Before you answer anything, go back to the ones you got wrong in the check "
            "and write down what you thought the answer was and why.",
            "Then work through these. If you get one wrong for the same reason, that is "
            "the thing to fix — not the question.",
        ],
        "revising": [
            "Short and specific: only what the check said was not there yet.",
            "Do them in one sitting, then re-take the check.",
        ],
        "exam_soon": [
            "The check found these, so they are worth more of your remaining time than "
            "anything you already get right.",
            "Answer them, then go straight back to the check.",
        ],
    },
    "foundation": {
        "first_time": [
            "This is groundwork from an earlier chapter, not the topic you asked about.",
            "Do these before anything else — the rest of the plan assumes them.",
            "If you get most of them right quickly, mark the step done and move on.",
        ],
        "revising": [
            "A quick check on something the topic below leans on.",
            "Five minutes here saves you guessing at which half of a later question went wrong.",
        ],
        "exam_soon": [
            "Your record says this earlier idea is shaky, and it sits underneath the topic.",
            "Give it one pass. If it holds up, skip straight to the practice below.",
        ],
    },
    "orient": {
        "first_time": [
            "Go through it once without taking notes — you are building a picture, not a record.",
            "Then go back and write down only the parts that surprised you.",
            "If a derivation loses you, mark where and keep going; the practice below will tell you whether it mattered.",
        ],
        "revising": [
            "Skim rather than study. You are finding what has faded, not learning it again.",
            "Anything you cannot say out loud in one sentence, note it down and let the practice below settle it.",
        ],
        "exam_soon": [
            "Do not go through this end to end. Use it only for the concepts named on this step.",
            "Give it fifteen minutes at most — this close to an exam, questions teach faster than explanations.",
        ],
    },
    "first_contact": {
        "first_time": [
            "Do the first three without looking anything up, even if you are stuck.",
            "Check all three at once rather than one at a time — a pattern of mistakes tells you more than a single one.",
            "For anything you got wrong, open Understand with AI before starting the next batch.",
        ],
        "revising": [
            "Work in batches of four and check each batch together.",
            "If a question takes more than two minutes, that is not revision, it is relearning — mark it and come back.",
        ],
        "exam_soon": [
            "Answer every one, including the ones you would normally skip. A guess still shows you where the gap is.",
            "Check the whole set in one go and write down only the concepts you missed.",
        ],
    },
    "weak_spots": {
        "first_time": [
            "These are the concepts your answers say are shakiest, not the ones that look hardest.",
            "Go slowly. Speed here just repeats the same mistake faster.",
            "Read the solution for every one you get wrong, including the ones you nearly had.",
        ],
        "revising": [
            "Straight to the concepts you have been missing — there is no warm-up in this step.",
            "After each wrong answer, say out loud what you would do differently before moving on.",
        ],
        "exam_soon": [
            "These are the cheapest marks you can still buy: concepts you have met and are getting wrong.",
            "Do them all, then re-do the ones you missed immediately rather than later.",
        ],
    },
    "exam_shape": {
        "first_time": [
            "These are real exam questions, so expect them to combine ideas rather than test one.",
            "Before answering, name which concept the question is really about; that habit is worth more than the answer.",
        ],
        "revising": [
            "Give yourself roughly a minute a question and stick to it.",
            "Check at the end, not as you go — stopping to check mid-set hides how you would actually perform.",
        ],
        "exam_soon": [
            "Sit these like the real thing: one sitting, no notes, no checking as you go.",
            "Afterwards, sort your mistakes into 'did not know' and 'knew but slipped'. They need different fixes.",
        ],
    },
    "consolidate": {
        "first_time": [
            "Go back over the material with your wrong answers next to you.",
            "For each mistake, find the line that would have prevented it — that is what to remember.",
        ],
        "revising": [
            "Re-read only the parts covering what you missed above.",
            "Close it and write the key relationships from memory before you look again.",
        ],
        "exam_soon": [
            "One pass, fifteen minutes, only the concepts you missed above.",
            "Write them on a single sheet you can look at the morning of the exam.",
        ],
    },
    "checkpoint": {
        "first_time": [
            "Sit this in one go, without notes, and without checking anything as you answer.",
            "Nothing is revealed until you have answered every question — that is deliberate, and it is the only way this tells you anything true.",
            "If you miss a few, the plan adds work on exactly those concepts. You do not start over.",
        ],
        "revising": [
            "One sitting, no notes, no checking as you go.",
            "This is the measurement the whole plan has been building to, so do not look anything up to protect the score.",
            "Whatever you miss becomes the next steps rather than a mark against you.",
        ],
        "exam_soon": [
            "Treat this as the exam: one sitting, timed by you, nothing open beside it.",
            "Answer everything, including guesses, exactly as you would on the day.",
            "What you miss here is what to spend your remaining time on.",
        ],
    },
}


def how_to_use(role: Role, intent: Intent | None) -> list[str]:
    """The instructions for this role, for a student with this intent.

    A missing or unrecognised intent falls back to `first_time`, which is the most
    explicit set — the right way to be wrong about somebody you know nothing about.
    """
    by_intent = GUIDANCE[role]
    return list(by_intent.get(intent or _DEFAULT_INTENT, by_intent[_DEFAULT_INTENT]))
