"""The OpenAI implementation of `PlannerProvider`.

Confined to this file on purpose. The structured-output surface has moved more than once,
and when it moves again this is the only module that should need touching — which is also
why the model id is configuration rather than a constant here.

Uses `chat.completions.parse` with a Pydantic model as the response format, so the reply
arrives already validated against the schema. That validates the *shape*; whether the ids
in it are real is `validate.py`'s job, and no amount of schema strictness substitutes for
that check.
"""

from __future__ import annotations

import logging
import time

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel

from app.providers.base import ProviderError, ProviderUsage

logger = logging.getLogger(__name__)

# One call, one wait. A student is watching a spinner, so a retry doubles the wait for a
# result that is usually not better — the repair round-trip and then the deterministic
# planner are the recovery path, and both are faster than a blind retry.
_TIMEOUT_SECONDS = 45.0
_MAX_RETRIES = 0

# Generous. Structured output for a six-step plan is small, but a truncated plan fails
# validation and costs a whole extra round trip to discover.
_MAX_OUTPUT_TOKENS = 8000


class OpenAIPlannerProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str):
        self.model = model
        self._client = AsyncOpenAI(
            api_key=api_key, timeout=_TIMEOUT_SECONDS, max_retries=_MAX_RETRIES
        )

    async def generate_plan(
        self,
        system_prompt: str,
        inventory_json: str,
        schema: type[BaseModel],
        repair_errors: list[str] | None = None,
    ) -> tuple[BaseModel, ProviderUsage]:
        # Static prompt first, volatile inventory last. Providers cache long identical
        # prefixes automatically, and that ordering is the whole of what makes it possible.
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": inventory_json},
        ]
        if repair_errors:
            from app.plans.prompt import repair_instruction

            messages.append({"role": "user", "content": repair_instruction(repair_errors)})

        started = time.monotonic()
        try:
            completion = await self._client.chat.completions.parse(
                model=self.model,
                messages=messages,
                response_format=schema,
                max_completion_tokens=_MAX_OUTPUT_TOKENS,
            )
        except OpenAIError as exc:
            # Never re-raise the provider's exception: its message can carry a slice of
            # the request, and this one travels up towards a log line.
            raise ProviderError(f"{type(exc).__name__} from {self.name}") from None
        elapsed = int((time.monotonic() - started) * 1000)

        choice = completion.choices[0]
        if choice.message.refusal:
            raise ProviderError("the model declined to answer")
        parsed = choice.message.parsed
        if parsed is None:
            raise ProviderError(f"no parseable plan (finish_reason={choice.finish_reason})")

        usage = completion.usage
        cached = 0
        if usage is not None and usage.prompt_tokens_details is not None:
            cached = usage.prompt_tokens_details.cached_tokens or 0
        return parsed, ProviderUsage(
            provider=self.name,
            model=completion.model,
            input_tokens=usage.prompt_tokens if usage else 0,
            output_tokens=usage.completion_tokens if usage else 0,
            cached_input_tokens=cached,
            latency_ms=elapsed,
        )


def build_provider(api_key: str | None, model: str | None):
    """The configured provider, or None — which is not an error.

    A missing key means the deterministic planner runs, every time, silently. A fresh
    Render instance without its environment set is a normal Tuesday, and it must degrade
    to a real plan rather than to a 500 on the one endpoint that costs money.
    """
    if not api_key or not model:
        logger.info("No planner provider configured; plans will be deterministic")
        return None
    return OpenAIPlannerProvider(api_key=api_key, model=model)
