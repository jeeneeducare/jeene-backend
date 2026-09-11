"""Building the model client Ask Jeene talks to, from configuration.

Its own builder rather than the planner's, because the two are configured apart on
purpose: the planner's flag and model decide whether plans are generated or deterministic,
and this one decides whether the doubt box exists. Moving one must never move the other —
a bad week for the planner is not a reason to take a student's doubts away, and vice versa.

Three `None`s, all of them ordinary rather than errors:
no flag, no key, no model. A fresh Render instance has none of them.
"""

from __future__ import annotations

import logging

from app.config import settings
from app.doubts import answer as answer_module
from app.providers.openai_provider import OpenAIPlannerProvider

logger = logging.getLogger(__name__)


def build_doubt_provider():
    """The configured provider, or None — which means the feature is simply off.

    None is not a failure and must never become a 500: the route answers "Ask Jeene is
    not available", which is the truth, and the rest of the chapter still works.
    """
    if not settings.jeene_doubts_enabled:
        return None
    if not settings.openai_api_key or not settings.jeene_doubts_model:
        logger.info("Ask Jeene is on but has no key or model; it will stay unavailable")
        return None
    return OpenAIPlannerProvider(
        api_key=settings.openai_api_key,
        model=settings.jeene_doubts_model,
        reasoning_effort=answer_module.REASONING_EFFORT,
    )
