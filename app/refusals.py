"""Refusals whose wording is meant for the student to read.

Most of what this API puts in `detail` is addressed to whoever is reading the logs.
"No published node 'chem_11_ch1' to plan for" is a true sentence and the wrong one to
show a fifteen year old, so the apps deliberately do not render it: `BackendException`
maps the status to a line of its own and keeps the server's words for diagnostics.

That rule is right for almost everything and wrong for a small, important minority.
Some refusals are the answer. "You already have 3 plans on the go, archive one first"
is not a fault to be translated into "something went wrong" — it is the thing the
student needs to know, and the only sentence that tells them what to do next.

Before this existed there was exactly one way to say so, and it was `402`: a gate body
that the apps render verbatim. That works, and it is why the paywall reads the same on
both platforms, but it is a paywall. Routing a shelf limit through it would have told a
student who already pays that they should pay, which is worse than the generic line it
replaced.

**Why this is a header and not a richer body.** The obvious design is to put an object
in `detail`, the way `402` does. It cannot be done that way without breaking the apps
already installed: every field of their `GateBlock` has a default and unknown keys are
ignored, so `{"audience": ..., "message": ...}` decodes into a *gate* with its message
set, and a build that predates this would draw a Pro upsell over a shelf limit. `detail`
therefore stays the plain string it has always been, and the fact that it may be shown
travels beside it. An app that has never heard of the header sees exactly what it sees
today; nothing regresses on a phone nobody has updated.
"""

from __future__ import annotations

from fastapi import HTTPException

#: Set on a refusal whose `detail` is written for the student and may be shown as-is.
AUDIENCE_HEADER = "X-Jeene-Audience"
STUDENT = "student"

#: Optionally set alongside it: the way out, for an app that wants to offer it rather
#: than only describe it. Advisory, and never required to understand the refusal.
ACTION_HEADER = "X-Jeene-Refusal-Action"
ARCHIVE_PLAN = "archive_plan"
WAIT = "wait"


def refuse(status: int, message: str, *, action: str | None = None) -> HTTPException:
    """A refusal the apps may show as written.

    Use it only where the sentence really is for the student and really does say what
    to do next. Anything else stays a plain `HTTPException`, which is the safe default:
    an app that shows the wrong sentence is worse than one that shows a general one.
    """
    headers = {AUDIENCE_HEADER: STUDENT}
    if action:
        headers[ACTION_HEADER] = action
    return HTTPException(status_code=status, detail=message, headers=headers)
