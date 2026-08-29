"""Whether a produced plan may be shown to a student.

Structured outputs guarantee the *shape* of what comes back. They say nothing about
whether a video id is real, whether a concept is in scope, or whether a selector can be
filled — and those are the failures that matter, because each of them is a step that
opens on nothing.

Two kinds of problem, treated differently on purpose:

  * **Repairable in place.** A count larger than the bucket holds, one foundation step too
    many, a learn step marked as graded. These are clamped or dropped here and recorded,
    because sending them back would cost a student several seconds to fix something this
    module can fix correctly in a microsecond.
  * **Fatal.** A video that does not exist, a concept outside the scope, a missing
    checkpoint, prose that reads like content. These are handed back to the provider once,
    named specifically, and if the second attempt fails too the deterministic planner runs.

Nothing here is a matter of taste. Every rule is either "the catalogue does not contain
that" or "a student would see something wrong".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.plans.schema import Inventory, PlanStep, StudyPlanOut

# What a step may promise before it stops being worth opening.
_MIN_SELECTOR_COUNT = 3

# Text that has no business in guidance. A planner that starts writing formulas has
# started teaching, and it is teaching from nothing — it was never given the content.
_CONTENT_PATTERNS = (
    (re.compile(r"\\[a-zA-Z]{2,}"), "LaTeX commands"),
    (re.compile(r"\$\$?[^$]{2,}\$\$?"), "LaTeX delimiters"),
    (re.compile(r"\b(the answer is|correct answer|option [a-d]\b)", re.I), "an answer"),
    (re.compile(r"[=≈≠≤≥]\s*[-+]?\d"), "an equation"),
    (re.compile(r"\b\d+(\.\d+)?\s*(m/s|km/h|N|J|kg|mol|Hz|°C|K|Pa|W|V|A)\b"),
     "a quantity with units"),
)

_MAX_WHY = 240
_MAX_INSTRUCTION = 220


@dataclass
class Verdict:
    """What must go back to the provider, and what was silently put right."""

    errors: list[str] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check(plan: StudyPlanOut, inventory: Inventory) -> Verdict:
    """Validate and repair `plan` in place against the catalogue it was built from."""
    verdict = Verdict()

    known_videos = {v.youtube_id for v in inventory.materials.videos}
    known_notes = {n.chapter_id for n in inventory.materials.notes}
    known_tests = {t.test_id for t in inventory.materials.tests}
    in_scope = (
        {n.node_id for n in inventory.concepts}
        | {n.node_id for n in inventory.subtopics}
        | {c.node_id for c in inventory.foundation_candidates}
        | {inventory.scope.node_id}
    )
    foundation_ok = {c.node_id for c in inventory.foundation_candidates}

    if not plan.steps:
        verdict.errors.append("The plan has no steps.")
        return verdict

    _check_length(plan, inventory, verdict)
    _check_foundation_budget(plan, inventory, foundation_ok, verdict)

    for index, step in enumerate(plan.steps):
        where = f"Step {index + 1} ({step.title!r})"
        _check_prose(step, where, verdict)
        _check_jargon(step, where, verdict)
        _check_completion(step, where, verdict)
        _check_dag(step, index, where, verdict)
        _check_nodes(step, in_scope, where, verdict)
        _check_items(step, inventory, known_videos, known_notes, known_tests,
                     in_scope, where, verdict)
        _check_kind(step, where, verdict)

    _check_checkpoint(plan, inventory, verdict)
    return verdict


# --- the rungs -------------------------------------------------------------------------


def _check_length(plan: StudyPlanOut, inventory: Inventory, verdict: Verdict) -> None:
    limits = inventory.constraints
    if len(plan.steps) > limits.max_steps:
        # Trimming from the end would take the checkpoint with it, so this goes back.
        verdict.errors.append(
            f"The plan has {len(plan.steps)} steps; at most {limits.max_steps} are allowed."
        )
    if len(plan.steps) < limits.min_steps:
        verdict.errors.append(
            f"The plan has {len(plan.steps)} steps; at least {limits.min_steps} are needed."
        )


def _check_foundation_budget(
    plan: StudyPlanOut, inventory: Inventory, allowed: set[str], verdict: Verdict
) -> None:
    """At most the budget, and only from the candidates offered.

    Over-budget is repaired by demoting the extras rather than deleting them: the step is
    still real work on a real node, it just stops being announced as groundwork. Deleting
    would shorten the plan below its floor and cascade into a second failure.
    """
    foundation = [s for s in plan.steps if s.is_foundation]
    for step in foundation:
        outside = [n for n in step.focus_node_ids if n not in allowed]
        if outside:
            verdict.errors.append(
                f"Step {plan.steps.index(step) + 1} is marked as groundwork on "
                f"{outside}, which is not in foundation_candidates."
            )
    budget = inventory.constraints.max_foundation_steps
    if len(foundation) > budget:
        for step in foundation[budget:]:
            step.is_foundation = False
        verdict.repaired.append(
            f"Demoted {len(foundation) - budget} foundation step(s) over the budget of {budget}."
        )


def _check_prose(step: PlanStep, where: str, verdict: Verdict) -> None:
    for text in [step.why, *step.how_to_use]:
        for pattern, what in _CONTENT_PATTERNS:
            if pattern.search(text):
                verdict.errors.append(
                    f"{where} writes {what} in its guidance. Guidance says what to do "
                    f"with the material, never what the material says."
                )
                break
    if len(step.why) > _MAX_WHY:
        step.why = step.why[:_MAX_WHY].rstrip()
        verdict.repaired.append(f"{where}: shortened `why`.")
    if not 2 <= len(step.how_to_use) <= 4:
        verdict.errors.append(
            f"{where} has {len(step.how_to_use)} instructions; two to four are required."
        )
    for i, line in enumerate(step.how_to_use):
        if len(line) > _MAX_INSTRUCTION:
            step.how_to_use[i] = line[:_MAX_INSTRUCTION].rstrip()
            verdict.repaired.append(f"{where}: shortened an instruction.")


# Words that belong to the catalogue, not to a student. A plan that tells somebody to
# "open the node" has leaked the shape of the data model into the product.
_JARGON = (
    ("the node", "node"), ("this node", "node"), ("bucket", "bucket"),
    ("the catalogue", "catalogue"), ("selector", "selector"),
    ("the scope", "scope"), ("inventory", "inventory"),
)


def _check_kind(step: PlanStep, where: str, verdict: Verdict) -> None:
    """A step labelled `learn` has to have something to learn from.

    The model reaches for `learn` on a step whose only items are questions, which reads
    to a student as a lesson and opens as a quiz. Relabelled rather than rejected: the
    step is good work, it is only wearing the wrong name.
    """
    if step.kind == "learn" and all(i.type == "questions" for i in step.items):
        step.kind = "practise"
        verdict.repaired.append(
            f"{where}: relabelled `learn` to `practise` — it has nothing to read or watch."
        )


def _check_jargon(step: PlanStep, where: str, verdict: Verdict) -> None:
    for text in [step.title, step.why, *step.how_to_use]:
        lowered = text.lower()
        for phrase, name in _JARGON:
            if phrase in lowered:
                verdict.errors.append(
                    f"{where} says {name!r} to the student. That is a word from the "
                    f"catalogue, not from studying — name the material instead."
                )
                return


def _check_completion(step: PlanStep, where: str, verdict: Verdict) -> None:
    """Only reading and watching may be completed by tapping.

    Repaired rather than rejected, and in the strict direction: a graded step wrongly
    marked self-markable is coerced to graded, never the other way round. Getting this
    wrong in the lenient direction would make the plan tappable, which is the one thing
    the whole feature cannot survive.
    """
    if step.completion.kind == "self" and step.kind not in ("learn", "consolidate"):
        step.completion.kind = "accuracy"
        step.completion.required_questions = step.completion.required_questions or 3
        step.completion.required_accuracy = step.completion.required_accuracy or 0.6
        verdict.repaired.append(f"{where}: a {step.kind} step cannot be self-marked.")

    if step.completion.kind != "self":
        if step.completion.required_questions is None:
            step.completion.required_questions = 3
            verdict.repaired.append(f"{where}: supplied a missing question bar.")
        if step.completion.required_accuracy is None:
            step.completion.required_accuracy = 0.6
            verdict.repaired.append(f"{where}: supplied a missing accuracy bar.")
        if not 0.5 <= step.completion.required_accuracy <= 0.9:
            step.completion.required_accuracy = min(
                0.9, max(0.5, step.completion.required_accuracy)
            )
            verdict.repaired.append(f"{where}: clamped the accuracy bar.")


def _check_dag(step: PlanStep, index: int, where: str, verdict: Verdict) -> None:
    """A step may only depend on earlier ones, which makes a cycle unrepresentable."""
    forward = [d for d in step.depends_on if d >= index or d < 0]
    if forward:
        step.depends_on = [d for d in step.depends_on if 0 <= d < index]
        verdict.repaired.append(f"{where}: dropped a dependency on a later step.")


def _check_nodes(step: PlanStep, in_scope: set[str], where: str, verdict: Verdict) -> None:
    outside = [n for n in step.focus_node_ids if n not in in_scope]
    if outside:
        verdict.errors.append(
            f"{where} focuses on {outside}, which is not in this scope or its groundwork."
        )


def _check_items(
    step: PlanStep,
    inventory: Inventory,
    videos: set[str],
    notes: set[str],
    tests: set[str],
    in_scope: set[str],
    where: str,
    verdict: Verdict,
) -> None:
    if not step.items:
        verdict.errors.append(
            f"{where} has no material to open. Every step must point at something."
        )
        return

    # An item whose filter cannot be filled is dropped rather than failing the plan —
    # but only while the step keeps at least one. Rejecting a six-step plan because one
    # sub-filter of one step came up two short spends a round trip, and then a student's
    # patience, on something that can be corrected here exactly.
    droppable = [i for i in step.items if i.type == "questions"]
    for item in list(step.items):
        if item.type == "video" and item.video_id not in videos:
            verdict.errors.append(
                f"{where} uses video {item.video_id!r}, which is not in the catalogue."
            )
        elif item.type == "notes" and item.notes_chapter_id not in notes:
            verdict.errors.append(
                f"{where} uses notes for {item.notes_chapter_id!r}, which do not exist."
            )
        elif item.type == "test" and item.test_id not in tests:
            verdict.errors.append(
                f"{where} uses test {item.test_id!r}, which is not in the catalogue."
            )
        elif item.type == "questions":
            _check_selector(
                step, item, inventory, in_scope, where, verdict,
                can_drop=len(step.items) > 1 and len(droppable) > 1,
            )


def _check_selector(step, item, inventory, in_scope, where, verdict, can_drop) -> None:
    selector = item.selector
    if selector is None:
        verdict.errors.append(f"{where} has a question item with no filter.")
        return

    outside = [n for n in selector.concept_node_ids if n not in in_scope]
    if outside:
        verdict.errors.append(
            f"{where} asks for questions on {outside}, which is not in this scope."
        )
        return

    available = _available(inventory, selector)
    if available < _MIN_SELECTOR_COUNT:
        if can_drop:
            step.items.remove(item)
            verdict.repaired.append(
                f"{where}: dropped a filter the catalogue holds only {available} "
                f"questions for."
            )
            return
        verdict.errors.append(
            f"{where} asks for questions that do not exist: the catalogue holds "
            f"{available} matching its filter. Widen the types or difficulties, or "
            f"name more concepts."
        )
        return
    if selector.count > available:
        # A clamp, not a rejection. The step is still worth doing at the size that exists,
        # and the frozen list is what the app renders anyway.
        verdict.repaired.append(
            f"{where}: reduced {selector.count} questions to {available}, which is all "
            f"the catalogue holds."
        )
        selector.count = available

    types = set(inventory.constraints.available_question_types)
    unknown = [t for t in selector.question_types if t not in types]
    if unknown:
        selector.question_types = [t for t in selector.question_types if t in types]
        verdict.repaired.append(f"{where}: dropped question types {unknown}.")


def _check_checkpoint(plan: StudyPlanOut, inventory: Inventory, verdict: Verdict) -> None:
    """The plan must end in a measurement, and the measurement must mean something."""
    last = plan.steps[-1]
    if last.completion.kind != "checkpoint" or last.kind != "verify":
        verdict.errors.append(
            "The last step must be a `verify` step with `completion.kind` of "
            "`checkpoint`. A plan that does not end in a measurement proves nothing."
        )
        return

    if any(s.completion.kind == "checkpoint" for s in plan.steps[:-1]):
        verdict.errors.append("Only the final step may be a checkpoint.")

    for item in last.items:
        if item.selector is None:
            continue
        if not item.selector.exclude_seen:
            item.selector.exclude_seen = True
            verdict.repaired.append(
                "Checkpoint: set exclude_seen. Measuring on questions the student has "
                "already answered measures memory of a deck, not the topic."
            )
        graded = [d for d in item.selector.difficulty if d != "unrated"]
        if item.selector.difficulty and not graded:
            verdict.errors.append(
                "The checkpoint asks only for ungraded questions. It claims to measure "
                f"at {inventory.constraints.target_difficulty!r}, and an ungraded "
                "question cannot support that claim."
            )


def _covered_buckets(inventory: Inventory, node_ids: list[str]) -> set[str]:
    """Which bucket keys these node ids stand for.

    A selector may name a concept, a subtopic, or the scope itself — `resolve.py` expands
    any of them to concept descendants, and the schema says so. Buckets are keyed by
    concept (or by subtopic when the inventory rolled up), so a validator that compared
    the selector's ids to bucket keys directly would reject a subtopic-wide step as
    asking for questions that do not exist. It did, and the plan it rejected was correct.
    """
    keys = {b.node_id for b in inventory.question_buckets}
    children: dict[str, list[str]] = {}
    for node in [*inventory.concepts, *inventory.subtopics]:
        if node.parent_id:
            children.setdefault(node.parent_id, []).append(node.node_id)

    covered: set[str] = set()
    for node_id in node_ids:
        # The scope stands for everything under it. Its intermediate levels — topics —
        # are not in the inventory at all, so walking down from a chapter would find
        # nothing; naming the whole set is both correct and simpler.
        if node_id == inventory.scope.node_id:
            covered |= keys
            continue
        pending = [node_id]
        while pending:
            current = pending.pop()
            if current in keys:
                covered.add(current)
            pending.extend(children.get(current, []))
    return covered


def _available(inventory: Inventory, selector) -> int:
    """How many questions could match this filter.

    An upper bound, deliberately. A question tagged to two of these concepts sits in two
    buckets, and a foundation candidate is only counted in total rather than by facet —
    so this over-estimates. That is the safe direction: `resolve.py` clamps against real
    rows when the step is opened, and the app renders the frozen count rather than this
    one. Under-estimating would reject plans that are perfectly good, which is exactly
    what the first version did.
    """
    types = set(selector.question_types)
    difficulty = set(selector.difficulty)

    covered = _covered_buckets(inventory, selector.concept_node_ids)
    total = sum(
        b.total
        for b in inventory.question_buckets
        if b.node_id in covered
        and (not types or b.question_type in types)
        and (not difficulty or b.difficulty in difficulty)
    )
    if inventory.scope_question_total:
        total = min(total, inventory.scope_question_total)

    # Groundwork sits outside the scope, so it has no buckets — only a count on the
    # candidate. Without this, every foundation step ever written is rejected as asking
    # for questions that do not exist, including the deterministic planner's own.
    wanted = set(selector.concept_node_ids)
    total += sum(
        c.question_count
        for c in inventory.foundation_candidates
        if c.node_id in wanted
    )
    return total
