"""How badly a student is doing at something, ranked so the worst is worth doing first.

Extracted from `mistakes.py` when the study planner needed the same ordering. The
Mistake Book ranks topics and the planner ranks concepts, but "where should the next
hour go" is one question and it should have one answer: a concept the planner calls
weak and a topic the Mistake Book puts second must not be scored by two different
rules that drifted apart. `figures.py` exists for the same reason.
"""

from __future__ import annotations

# One-sided 90% confidence. Low enough that a topic seen a handful of times can still
# surface, which matters when a student has only just started, and high enough that a
# single unlucky question does not top the list.
WILSON_Z = 1.2816


def wilson_lower_bound(wrong: int, attempted: int) -> float:
    """How high the error rate can be said to be, conservatively.

    A topic with one miss out of one has an observed error rate of 1.0 and almost no
    evidence behind it; this returns about 0.38 for that against about 0.60 for eight
    misses out of ten, which is the ordering the screen wants.
    """
    if attempted <= 0:
        return 0.0
    p = wrong / attempted
    z2 = WILSON_Z * WILSON_Z
    centre = p + z2 / (2 * attempted)
    spread = WILSON_Z * ((p * (1 - p) + z2 / (4 * attempted)) / attempted) ** 0.5
    return max(0.0, (centre - spread) / (1 + z2 / attempted))


def worth_doing(wrong: int, attempted: int) -> float:
    """How much a topic deserves to be at the top of the list.

    Confidence that the gap is real, multiplied by how much of it is left. The square
    root keeps the second factor from taking over: a topic with sixteen outstanding is
    weighted four times one with a single question, not sixteen times, so a small topic
    the student is failing outright still gets seen.
    """
    return wilson_lower_bound(wrong, attempted) * (wrong ** 0.5)
