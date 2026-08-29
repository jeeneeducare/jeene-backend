"""Producing a plan: the ladder, and the one place a provider is spoken to.

Three rungs, and the bottom one always succeeds.

  1. **Generate.** One call.
  2. **Repair.** One more, carrying the specific reasons the first was rejected. Not a
     retry — an identical request most likely produces an identical answer.
  3. **Fall back.** The deterministic planner. Logged, and invisible to the student.

Two is the ceiling. A third attempt spends several more seconds of a student's time on a
small chance of a better plan, and `fallback.py` is good enough that it is a bad trade.

Every path through here returns a plan. There is no arrangement of provider outage,
malformed output, missing key or disabled flag that produces an error — which is the only
reason it was safe to put a paid third-party call on the path a student waits for.
"""

from __future__ import annotations

import logging
import time

from app.plans import fallback, validate
from app.plans.prompt import PROMPT_VERSION, SYSTEM
from app.plans.schema import Inventory, StudyPlanOut
from app.providers.base import PlannerProvider, ProviderError, ProviderUsage

logger = logging.getLogger(__name__)


class Generated:
    """A plan and how it came about. `origin` is what makes a support question answerable."""

    __slots__ = ("plan", "origin", "provider", "model", "prompt_version", "usage")

    def __init__(self, plan: StudyPlanOut, origin: str, prompt_version: int,
                 provider: str | None = None, model: str | None = None,
                 usage: ProviderUsage | None = None):
        self.plan = plan
        self.origin = origin
        self.provider = provider
        self.model = model
        self.prompt_version = prompt_version
        self.usage = usage


async def generate(
    inventory: Inventory,
    provider: PlannerProvider | None,
    *,
    enabled: bool = True,
) -> Generated:
    """A plan for this inventory, whatever it takes."""
    if provider is None or not enabled:
        return _fallback(inventory, reason="no provider" if provider is None else "disabled")

    # Serialised once. This exact string is what crossed the boundary, and it is the only
    # thing besides the frozen system prompt that the provider sees.
    inventory_json = inventory.model_dump_json()
    started = time.monotonic()
    errors: list[str] = []

    for attempt in (1, 2):
        try:
            plan, usage = await provider.generate_plan(
                SYSTEM,
                inventory_json,
                StudyPlanOut,
                repair_errors=errors if attempt == 2 else None,
            )
        except ProviderError as exc:
            logger.warning(
                "planner provider failed scope=%s attempt=%d reason=%s",
                inventory.scope.node_id, attempt, exc,
            )
            return _fallback(inventory, reason=f"provider error on attempt {attempt}")

        verdict = validate.check(plan, inventory)
        _log(inventory, attempt, usage, verdict)

        if verdict.ok:
            return Generated(
                plan=plan,
                origin="model",
                prompt_version=PROMPT_VERSION,
                provider=provider.name,
                model=usage.model,
                usage=usage,
            )
        errors = verdict.errors

    logger.warning(
        "planner output rejected twice scope=%s after=%dms failures=%s",
        inventory.scope.node_id,
        int((time.monotonic() - started) * 1000),
        _failure_kinds(errors),
    )
    return _fallback(inventory, reason="rejected twice")


def _fallback(inventory: Inventory, reason: str) -> Generated:
    """The deterministic plan. Not an error path — it is what ships enabled off."""
    logger.info(
        "deterministic plan scope=%s reason=%s", inventory.scope.node_id, reason
    )
    return Generated(
        plan=fallback.plan(inventory),
        origin="fallback",
        prompt_version=PROMPT_VERSION,
    )


def _log(inventory: Inventory, attempt: int, usage: ProviderUsage, verdict) -> None:
    """One line per call.

    Deliberately no prompt and no response text. This whole feature exists to keep the
    question bank away from a third party; a log that accumulated the inventory would be
    a second copy of exactly what the boundary refuses to send, sitting somewhere with
    weaker access control than the database.

    `cached` is worth watching: it going to zero on a warm route means something has
    started varying in the static half of the prompt.
    """
    logger.info(
        "planner call scope=%s attempt=%d model=%s in=%d out=%d cached=%d ms=%d "
        "ok=%s repaired=%d failures=%s",
        inventory.scope.node_id,
        attempt,
        usage.model,
        usage.input_tokens,
        usage.output_tokens,
        usage.cached_input_tokens,
        usage.latency_ms,
        verdict.ok,
        len(verdict.repaired),
        _failure_kinds(verdict.errors),
    )


def _failure_kinds(errors: list[str]) -> str:
    """A compact, content-free summary of what went wrong.

    The messages name node and video ids, which are not content but are more than a log
    line needs. Counting them by opening phrase is enough to answer "is the model getting
    worse" without keeping any of the catalogue.
    """
    if not errors:
        return "none"
    return ";".join(sorted({e.split(" which ")[0].split(":")[0][:48] for e in errors}))
