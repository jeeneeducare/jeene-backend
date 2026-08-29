"""What a planning provider has to be, and what it has to report back.

Deliberately small. A provider is handed a frozen system prompt, an inventory already
serialised by the boundary, and the schema the answer must satisfy. It returns a parsed
object and an account of what the call cost. It decides nothing about what a good plan
looks like and it is never given a chance to: the prompt is not its to write, the
inventory is not its to assemble, and the result is validated by somebody else.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import BaseModel


class ProviderUsage(BaseModel):
    """What one call cost, for the log line and for nothing else.

    No prompt and no response text. The whole feature exists to keep question content
    away from a third party, and a log that quietly accumulates the inventory would be a
    second copy of exactly what the boundary refuses to send.
    """

    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    # Providers cache long identical prefixes automatically. This is how you find out
    # whether the static half of the prompt is actually stable — a timestamp anywhere in
    # it silently drives this to zero.
    cached_input_tokens: int = 0
    latency_ms: int = 0


class ProviderError(RuntimeError):
    """The provider could not answer. Always recoverable: the caller falls back."""


@runtime_checkable
class PlannerProvider(Protocol):
    name: str
    model: str

    async def generate_plan(
        self,
        system_prompt: str,
        inventory_json: str,
        schema: type[BaseModel],
        repair_errors: list[str] | None = None,
    ) -> tuple[BaseModel, ProviderUsage]:
        """Produce a plan matching `schema`.

        `repair_errors`, when given, are the specific reasons the previous attempt was
        rejected. Passing them is what makes the second call a repair rather than a
        retry: an identical request would most likely produce an identical answer.
        """
        ...
