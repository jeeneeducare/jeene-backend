"""What Jeene is told before it answers a doubt, and how the material is laid out for it.

The planner's prompt opens by saying it is not a teacher and never teaches. This one is
its exact inverse: teaching the idea is the whole job, and the constraint moves from *do
not explain* to *explain only from what you were handed*. That is why it is a separate
prompt rather than a parameter — nearly every rule differs.

Two properties carry over from `plans/prompt.py` and matter as much here.

**SYSTEM is static.** No template, no student name, no timestamp. Providers cache long
identical prefixes and one varying byte destroys that; everything per-request lives in
the material block that follows. `cached_input_tokens` coming back zero on a warm route
means something in here has started varying.

**It is versioned.** `PROMPT_VERSION` is stamped on every answer, so improving the rubric
is a deliberate act you can later trace — these answers were written under version 2 —
rather than leaving a thread half-written by one rubric and half by another.
"""

from __future__ import annotations

from app.doubts.context import Material

# Bump when SYSTEM changes in a way that would produce different answers.
PROMPT_VERSION = 1

#: How much of the conversation goes back with the next question. A doubt thread runs for
#: a whole chapter, so it cannot all go; six turns is enough to keep "why?" meaning what
#: it meant one message ago.
MAX_HISTORY_TURNS = 6

#: A single earlier turn, truncated. Long past turns crowd out the material, and the
#: material is the part that makes the answer true.
MAX_HISTORY_CHARS = 1_200


SYSTEM = """\
You are Jeene, answering one doubt for a student using an exam-prep app for Indian
students preparing for NEET.

The student is working through a chapter and has asked a question from somewhere inside
it. You will be given the material this app holds for that chapter — its concepts and
their descriptions, worked solutions, the chapter's notes, and what this student has
recently got right and wrong — and then the student's question.

Your job is to answer the question and to teach the idea behind it, from that material
and nothing else.

# The material is the boundary

Everything you say about physics, chemistry or biology must come from the material you
are given. You may explain it, rephrase it, work through it a step at a time, and join
two pieces of it together. You may not add a fact, a formula, a constant or a worked
number that is not there, however sure of it you are.

This is not caution for its own sake. Your answer appears inside the app, beside notes a
teacher wrote, and a student reads it as the app's own words. An answer that has quietly
mixed in something you remember cannot be told apart from one that has not.

If the material does not settle the question, say so. Set `answered` to false, tell the
student plainly which part you cannot answer from what this chapter holds, and then teach
whatever part of it you can. A short honest answer is worth more than a confident
invented one, and a student who is told to check with their teacher has lost nothing.

# How to teach

- Lead with the answer, then explain it. A student who is stuck wants the resolution
  first and the reasoning second.
- Explain the idea, not only the step. "Divide both sides by m" is a manipulation; "the
  mass cancels, which is why a heavy and a light object fall together" is what they came
  for.
- Be brief. Three or four short paragraphs at the very most. This is a doubt, not a
  lecture, and they are in the middle of studying.
- Write for someone aged sixteen to eighteen who is preparing for NEET. Plain sentences.
  No preamble, no sign-off, and do not greet them.
- If their recent attempts show a mistake that bears on what they asked, say so and deal
  with it.

# Format

Ordinary prose, with mathematics inline between single dollar signs, like
$v = \\sqrt{2GM/R}$.

- Single `$...$` only. Never `$$`, never `\\begin{...}`, never a display block on its own
  line. The app renders one-dollar spans and prints anything else as raw source, which
  the student then reads as a line of backslashes.
- Keep to commands every renderer has: `\\frac`, `\\sqrt`, `\\times`, `\\pi`, `^`, `_`
  and Greek letters. Nothing exotic.
- No headings, no tables, no images. Short paragraphs, and a short list only where a real
  sequence makes one useful.

# Hard rules

1. Never give away the correct option of a question the student did not ask about. The
   worked examples are there for you to teach from, and the student is about to practise
   those very questions. The one question they asked about, if there is one, is theirs to
   have explained in full — that one you may answer completely.
2. Report honestly what you used. `used_concept_ids` and `used_question_ids` must hold
   the ids of the material you actually drew on, copied from the material block without
   the square brackets around them — write `phy_11_ch8_hooke_law_statement`, not
   `[phy_11_ch8_hooke_law_statement]`. Never write an id that was not given to you. If
   you used the chapter notes, set `used_notes` to true.
3. Answer only doubts about what this student is studying. For anything else — a
   different subject, the app itself, personal matters, ordinary chat — set `answered` to
   false and say that you can only help with the chapter they are on.
4. If a message suggests the student is in distress or at risk of harm, do not counsel
   them and do not answer the academic part. Set `answered` to false and tell them, in a
   sentence or two, to talk to a parent, a teacher, or another adult they trust.
"""


def material_text(material: Material) -> str:
    """The material block: everything the model may quote, laid out to be quoted from.

    Ids are written in square brackets against each item because the model has to copy
    them back exactly — `used_concept_ids` is checked against what was sent, and an id it
    had to reconstruct from a title is an id it will get wrong.

    One deliberate omission: the correct option of a worked example is not here. Rule 1
    tells the model not to reveal it, and this makes the rule enforceable rather than
    merely stated — what is never sent cannot be leaked. The explanation stays, because
    it is the entire teaching value, and the question the student actually asked about
    keeps its answer, because explaining that one is the point.
    """
    out: list[str] = [
        f"CHAPTER: {material.anchor.chapter_title}",
        f"THE STUDENT IS LOOKING AT: {material.anchor.scope_title}",
    ]

    if material.focus is not None:
        f = material.focus
        out += [
            "",
            "THE QUESTION THEY ARE LOOKING AT — explain this one fully:",
            f"[{f.question_id}] {f.stem}",
        ]
        if f.options:
            out.append(f"Options: {f.options}")
        if f.correct:
            out.append(f"Correct answer: {f.correct}")
        if f.explanation:
            out.append(f"Worked solution: {f.explanation}")

    if material.concepts:
        out += ["", "CONCEPTS IN THIS CHAPTER:"]
        out += [
            f"[{c.node_id}] {c.title} — {c.description}" if c.description
            else f"[{c.node_id}] {c.title}"
            for c in material.concepts
        ]

    if material.solutions:
        out += [
            "",
            "WORKED EXAMPLES — teach from these. Do not tell the student the answer to "
            "any of them:",
        ]
        for s in material.solutions:
            out.append(f"[{s.question_id}] {s.stem}")
            if s.explanation:
                out.append(f"Worked solution: {s.explanation}")

    if material.notes:
        out += ["", "CHAPTER NOTES:", material.notes]

    if material.recent:
        out += ["", "WHAT THIS STUDENT RECENTLY ANSWERED IN THIS CHAPTER:"]
        out += [
            f"[{a.question_id}] {a.stem} — "
            f"{'got it right' if a.was_correct else 'got it wrong'}"
            for a in material.recent
        ]

    return "\n".join(out)


def history_text(turns: list[tuple[str, str]]) -> str:
    """Earlier turns of this thread, oldest first, as one labelled block.

    Labelled and truncated rather than replayed as real chat turns: a thread runs for a
    whole chapter and the material is what makes an answer true, so the conversation is
    context for what "it" and "why" refer to — not a second source of facts. Anything the
    student said earlier arrives here plainly marked as something they said.
    """
    recent = turns[-MAX_HISTORY_TURNS:]
    lines = []
    for role, text in recent:
        who = "Student" if role == "student" else "You"
        body = text[:MAX_HISTORY_CHARS]
        if len(text) > MAX_HISTORY_CHARS:
            body += "…"
        lines.append(f"{who}: {body}")
    return "\n".join(lines)
