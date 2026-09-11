"""What Jeene is allowed to read before it answers.

This module decides, for one question asked from one place in the app, exactly which
concepts, worked solutions and notes go to the model — and nothing else does. It is the
part of the feature worth being slow and careful about: if the right material is chosen a
plain prompt gives good answers, and if the wrong material is chosen no prompt can save it.

Three ideas shape it.

**The anchor is most of the prompt.** A student never asks into a blank page; they ask from
a question card, a notes reader, a topic sheet or a chapter screen. Where they asked from
says what they are asking about, far more reliably than the words they used.

**Concepts always, solutions selectively.** Every published concept in this app has a
written description — all 1,517 of them — and they average 226 characters. A whole
chapter's concepts are about 2,800 tokens; that chapter's worked solutions are 16,000. So
the concepts go in whole and the solutions are chosen, which is what keeps a chapter-wide
question affordable without building a search engine to do it.

**No search engine.** There are no embeddings and no full-text index here, and this needs
neither: the student is standing on a scope, and relevance within that scope is a token
overlap against concept titles and descriptions — the same matcher `plans/lookup.py`
already uses to turn a typed phrase into a syllabus node.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import asyncpg

from app import notes_text
from app.plans.lookup import normalise

logger = logging.getLogger(__name__)

#: Anchors, in the order they narrow. A question is the most specific thing a student can
#: be looking at; a node — chapter, topic, subtopic or concept — is the least.
ANCHOR_KINDS = ("question", "notes", "node")

#: A chapter can hold well over a hundred concepts. Past this the prompt stops being
#: grounding and starts being a haystack, and the model's attention is finite.
MAX_CONCEPTS = 60

#: Solutions are the expensive half. Twelve is enough to show a pattern — several worked
#: examples of the thing being asked about — without paying for a chapter's worth.
MAX_SOLUTIONS = 12

#: What the student has just been doing in this chapter. Enough to notice "they got the
#: last two wrong, both on sign conventions"; not so much that it crowds out the material.
MAX_RECENT_ATTEMPTS = 6

#: A ceiling on the whole assembly, trimmed from the solutions end because concepts and
#: notes are the small, complete, valuable part. Roughly 8k tokens.
MAX_MATERIAL_CHARS = 32_000


@dataclass(frozen=True)
class Concept:
    node_id: str
    title: str
    description: str


@dataclass(frozen=True)
class Solution:
    question_id: str
    stem: str
    options: str
    correct: str
    explanation: str


@dataclass(frozen=True)
class Attempt:
    question_id: str
    stem: str
    was_correct: bool


@dataclass(frozen=True)
class Anchor:
    """Where the student asked from, resolved to something the database agrees exists."""

    kind: str
    anchor_id: str
    #: Every anchor resolves to a chapter, because the thread is per chapter.
    chapter_id: str
    chapter_title: str
    #: What the student would say they are looking at — a question, a chapter, a subtopic.
    scope_title: str
    #: The node the material is gathered under. For a question anchor this is the chapter,
    #: because a question's own concepts are added separately and are usually too narrow
    #: on their own to explain anything.
    scope_node_id: str


@dataclass
class Material:
    """Everything the model may quote, and nothing it may not."""

    anchor: Anchor
    concepts: list[Concept] = field(default_factory=list)
    solutions: list[Solution] = field(default_factory=list)
    notes: str = ""
    recent: list[Attempt] = field(default_factory=list)
    #: The question the student is looking at, when they asked from one.
    focus: Solution | None = None

    def citation_labels(self) -> dict[str, str]:
        """Short references — `C1`, `Q1` — against the real ids they stand for.

        The model is asked to copy one of these back, not an id. Ids in this syllabus run
        to fifty characters of nested segments: `chem_11_ch4_vb_theory_h2_formation_
        overlap_concept` sits beside `chem_11_ch4_vb_theory_overlap_types_overlap_signs`,
        and asking for sixty of those transcribed exactly is asking for a slip. It got
        one: a real answer about sigma and pi bonds was binned four times out of four
        because the reply cited `chem_11_ch4_vb_theory_overlap_concept` — two real ids
        conflated, a middle segment dropped.

        A two-character token cannot be mis-transcribed that way, and the check it feeds
        is unweakened: `C99` is still not a reference that was given. The real ids also
        stop leaving the server, which is worth having on its own.

        Ordered exactly as `prompt.material_text` lists them, because both derive their
        numbering from this one function rather than agreeing by hand.
        """
        labels: dict[str, str] = {}
        for index, concept in enumerate(self.concepts, start=1):
            labels[f"C{index}"] = concept.node_id
        number = 1
        if self.focus is not None:
            labels[f"Q{number}"] = self.focus.question_id
            number += 1
        for solution in self.solutions:
            labels[f"Q{number}"] = solution.question_id
            number += 1
        return labels

    def concept_ids(self) -> set[str]:
        return {c.node_id for c in self.concepts}

    def question_ids(self) -> set[str]:
        ids = {s.question_id for s in self.solutions}
        if self.focus is not None:
            ids.add(self.focus.question_id)
        return ids

    def is_empty(self) -> bool:
        """Nothing to answer from. The caller refuses rather than asking anyway."""
        return not self.concepts and not self.solutions and not self.notes

    def approx_chars(self) -> int:
        return (
            sum(len(c.title) + len(c.description) for c in self.concepts)
            + sum(len(s.stem) + len(s.explanation) for s in self.solutions)
            + len(self.notes)
        )


# --- resolving where they asked from ----------------------------------------------------

_CHAPTER_OF = """
WITH RECURSIVE up AS (
    SELECT node_id, parent_id, type, title
      FROM nodes WHERE node_id = $1 AND tenant_id = $2 AND status = 'published'
    UNION ALL
    SELECT n.node_id, n.parent_id, n.type, n.title
      FROM nodes n JOIN up ON up.parent_id = n.node_id
     WHERE n.tenant_id = $2 AND n.status = 'published'
)
SELECT node_id, title FROM up WHERE type = 'chapter' LIMIT 1
"""


async def resolve(
    connection: asyncpg.Connection, kind: str, anchor_id: str, tenant: str
) -> Anchor | None:
    """Turn "they asked from here" into something the database vouches for.

    None for an anchor that does not exist, is not published, or hangs under no chapter.
    The caller answers that with a refusal rather than a guess — an anchor the student
    could not have been looking at is not a question worth spending a model call on.
    """
    if kind == "question":
        return await _question_anchor(connection, anchor_id, tenant)
    if kind == "notes":
        return await _notes_anchor(connection, anchor_id, tenant)
    if kind == "node":
        return await _node_anchor(connection, anchor_id, tenant)
    return None


async def _question_anchor(
    connection: asyncpg.Connection, question_id: str, tenant: str
) -> Anchor | None:
    row = await connection.fetchrow(
        """
        SELECT q.question_id, n.node_id AS chapter_id, n.title AS chapter_title
          FROM questions q
          JOIN question_concept_mappings m ON m.question_id = q.question_id
          JOIN LATERAL (
                WITH RECURSIVE up AS (
                    SELECT node_id, parent_id, type, title FROM nodes
                     WHERE node_id = m.concept_node_id AND tenant_id = q.tenant_id
                    UNION ALL
                    SELECT p.node_id, p.parent_id, p.type, p.title
                      FROM nodes p JOIN up ON up.parent_id = p.node_id
                     WHERE p.tenant_id = q.tenant_id
                )
                SELECT node_id, title FROM up WHERE type = 'chapter' LIMIT 1
          ) n ON TRUE
         WHERE q.question_id = $1 AND q.tenant_id = $2 AND q.status = 'published'
         -- Primary mapping first. A question is often mapped to several concepts and
         -- occasionally they sit in different chapters; the one it is *primarily* about
         -- is the chapter the student is in, and picking whichever row came back first
         -- would answer a gravitation doubt out of the wrong chapter now and then.
         ORDER BY m.is_primary DESC, m.concept_node_id
         LIMIT 1
        """,
        question_id, tenant,
    )
    if row is None:
        return None
    return Anchor(
        kind="question", anchor_id=question_id,
        chapter_id=row["chapter_id"], chapter_title=row["chapter_title"],
        # A question's own concepts are narrow by design — often one idea. Gathering under
        # the chapter gives the model somewhere to explain *from*, and the question's own
        # material is added separately as the focus.
        scope_title=row["chapter_title"], scope_node_id=row["chapter_id"],
    )


async def _notes_anchor(
    connection: asyncpg.Connection, chapter_id: str, tenant: str
) -> Anchor | None:
    row = await connection.fetchrow(
        """
        SELECT cn.chapter_id, n.title
          FROM chapter_notes cn JOIN nodes n ON n.node_id = cn.chapter_id
         WHERE cn.chapter_id = $1 AND cn.tenant_id = $2 AND cn.status = 'published'
        """,
        chapter_id, tenant,
    )
    if row is None:
        return None
    return Anchor(
        kind="notes", anchor_id=chapter_id,
        chapter_id=row["chapter_id"], chapter_title=row["title"],
        scope_title=f"the notes on {row['title']}", scope_node_id=row["chapter_id"],
    )


async def _node_anchor(
    connection: asyncpg.Connection, node_id: str, tenant: str
) -> Anchor | None:
    node = await connection.fetchrow(
        "SELECT node_id, title, type FROM nodes "
        " WHERE node_id = $1 AND tenant_id = $2 AND status = 'published'",
        node_id, tenant,
    )
    if node is None:
        return None
    chapter = await connection.fetchrow(_CHAPTER_OF, node_id, tenant)
    if chapter is None:
        # A subject or a class level. Nothing hangs a doubt on those.
        return None
    return Anchor(
        kind="node", anchor_id=node_id,
        chapter_id=chapter["node_id"], chapter_title=chapter["title"],
        scope_title=node["title"], scope_node_id=node_id,
    )


# --- gathering ---------------------------------------------------------------------------

_CONCEPTS_UNDER = """
WITH RECURSIVE down AS (
    SELECT node_id, parent_id, type, title, description, display_order,
           ARRAY[coalesce(display_order, 0)] AS ordering
      FROM nodes WHERE node_id = $1 AND tenant_id = $2 AND status = 'published'
    UNION ALL
    SELECT n.node_id, n.parent_id, n.type, n.title, n.description, n.display_order,
           d.ordering || coalesce(n.display_order, 0)
      FROM nodes n JOIN down d ON n.parent_id = d.node_id
     WHERE n.tenant_id = $2 AND n.status = 'published'
)
SELECT node_id, title, coalesce(description, '') AS description
  FROM down
 WHERE type = 'concept' AND coalesce(description, '') <> ''
 ORDER BY ordering
 LIMIT $3
"""

#: `array_position` is what makes the ranking mean anything.
#:
#: `= ANY($array)` matches without caring about order, so the first version of this
#: computed a relevance score, passed the concepts in ranked order, and then let Postgres
#: return whichever twelve rows it reached first. Asking "what is escape velocity" came
#: back with Kepler's laws — the same material as asking nothing at all.
#:
#: The inner `DISTINCT ON` keeps one row per question at its *best*-ranked concept, since
#: a question mapped to four concepts should be judged by the closest one. The outer sort
#: then takes the questions belonging to the concepts the student actually asked about.
_SOLUTIONS_FOR = """
SELECT question_id, question_text, options_json, correct_option_ids, explanation_json
  FROM (
    SELECT DISTINCT ON (q.question_id)
           q.question_id, q.question_text, q.options_json, q.correct_option_ids,
           q.explanation_json,
           array_position($2::text[], m.concept_node_id) AS rank
      FROM questions q
      JOIN question_concept_mappings m ON m.question_id = q.question_id
     WHERE q.tenant_id = $1 AND q.status = 'published'
       AND q.explanation_json IS NOT NULL
       AND m.concept_node_id = ANY($2::text[])
     ORDER BY q.question_id, rank
  ) best
 ORDER BY rank, question_id
 LIMIT $3
"""

_ONE_SOLUTION = """
SELECT question_id, question_text, options_json, correct_option_ids, explanation_json
  FROM questions
 WHERE question_id = $1 AND tenant_id = $2 AND status = 'published'
"""

# One row per question, not one per mapping. A question is usually mapped to several
# concepts, and the obvious join returns it once for each — so a student who answered two
# questions would be shown six "recent attempts", most of them the same two repeated.
_RECENT = """
SELECT question_id, question_text, is_correct
  FROM (
    SELECT DISTINCT ON (a.question_id)
           a.question_id, q.question_text, a.is_correct, a.created_at
      FROM attempts a
      JOIN questions q ON q.question_id = a.question_id AND q.tenant_id = a.tenant_id
      JOIN question_concept_mappings m ON m.question_id = a.question_id
     WHERE a.firebase_uid = $1
       AND a.tenant_id = $2
       AND m.concept_node_id = ANY($3::text[])
     ORDER BY a.question_id, a.created_at DESC
  ) latest
 ORDER BY created_at DESC
 LIMIT $4
"""


async def gather(
    connection: asyncpg.Connection,
    anchor: Anchor,
    question: str,
    uid: str,
    tenant: str,
) -> Material:
    """Everything Jeene may quote when answering this question, from this place.

    The student's words are used only to *rank* material inside the anchor, never to
    search outside it. That is deliberate: a doubt about gravitation that happens to use
    the word "field" must not drag in electrostatics.
    """
    material = Material(anchor=anchor)

    concepts = await connection.fetch(
        _CONCEPTS_UNDER, anchor.scope_node_id, tenant, MAX_CONCEPTS
    )
    material.concepts = [
        Concept(node_id=r["node_id"], title=r["title"], description=r["description"])
        for r in concepts
    ]

    # The chapter's notes, whole. Measured across all eight documents that exist: the
    # largest is under 2,000 tokens, so there is nothing here worth chunking.
    material.notes = await notes_text.text_for(connection, anchor.chapter_id, tenant)

    if anchor.kind == "question":
        row = await connection.fetchrow(_ONE_SOLUTION, anchor.anchor_id, tenant)
        material.focus = _as_solution(row) if row else None

    # Ranked once and used twice: the concepts are re-ordered so the prompt reads the
    # relevant ones first, and the same order decides which worked solutions come back.
    ranked = _rank(material.concepts, question)
    if ranked:
        by_id = {c.node_id: c for c in material.concepts}
        material.concepts = [by_id[node_id] for node_id in ranked]

        rows = await connection.fetch(
            _SOLUTIONS_FOR, tenant, ranked, MAX_SOLUTIONS
        )
        focus_id = material.focus.question_id if material.focus else None
        material.solutions = [
            s for s in (_as_solution(r) for r in rows) if s.question_id != focus_id
        ]

        material.recent = [
            Attempt(question_id=r["question_id"], stem=r["question_text"],
                    was_correct=r["is_correct"])
            for r in await connection.fetch(
                _RECENT, uid, tenant, [c.node_id for c in material.concepts],
                MAX_RECENT_ATTEMPTS,
            )
        ]

    _trim(material)
    return material


def _rank(concepts: list[Concept], question: str) -> list[str]:
    """Concept ids, most likely to be what they asked about first.

    Token overlap against the title and description, which is the same idea the scope
    lookup uses. Crude, and it does not need to be better: it is choosing *within* a scope
    the student has already chosen, so the wrong order costs a little context rather than
    the wrong subject.

    With no usable words to go on — "i don't understand this" — every concept scores zero
    and the natural order stands, which is the order the chapter is taught in.
    """
    if not concepts:
        return []
    _, _, tokens = normalise(question)
    if not tokens:
        return [c.node_id for c in concepts]

    wanted = set(tokens)

    def score(concept: Concept) -> int:
        title = set(normalise(concept.title)[2])
        body = set(normalise(concept.description)[2])
        # A hit in the title is worth more than one in the body: a description mentions
        # many things in passing and is named after exactly one.
        return 3 * len(wanted & title) + len(wanted & body)

    ordered = sorted(concepts, key=score, reverse=True)
    return [c.node_id for c in ordered]


def _trim(material: Material) -> None:
    """Hold the assembly under its ceiling, dropping solutions first.

    Concepts and notes are small, complete and the reason the answer can be grounded at
    all. Solutions are numerous and individually replaceable, so they are what gives way.
    """
    while material.solutions and material.approx_chars() > MAX_MATERIAL_CHARS:
        material.solutions.pop()


def _as_solution(row: asyncpg.Record) -> Solution:
    explanation = row["explanation_json"]
    if isinstance(explanation, str):
        try:
            explanation = json.loads(explanation)
        except ValueError:
            explanation = {}
    text = (explanation or {}).get("text", "") if isinstance(explanation, dict) else ""

    options = row["options_json"]
    if isinstance(options, str):
        try:
            options = json.loads(options)
        except ValueError:
            options = []
    rendered = "  ".join(
        f"({o.get('option_id', '?')}) {o.get('text', '')}"
        for o in (options or [])
        if isinstance(o, dict)
    )

    return Solution(
        question_id=row["question_id"],
        stem=row["question_text"] or "",
        options=rendered,
        correct=", ".join(row["correct_option_ids"] or []),
        explanation=text,
    )
