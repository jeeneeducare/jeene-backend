"""Run the doubt cases against the real pipeline and say whether it is fit to ship.

Not a test. It costs money, needs a model and a database, and its answers are prose that
varies run to run — so it lives here rather than in `tests/`, is run deliberately, and
reports rather than passes.

What it measures, in the order the numbers matter:

  **grounding** — answers that survived the citation check. This is the safety property:
                  a dropped answer means the model cited material it was never given, and
                  it is a failure of the system rather than a refusal.
  **labels**    — did it answer what it exists for, and refuse what it must? Both
                  directions count. A feature that refuses everything scores perfectly on
                  safety and is worthless.
  **format**    — maths the app can actually draw. `$$`, `\\begin{}` and an odd number of
                  `$` all render as raw source in front of a student.
  **cost**      — rupees per answer at the ten-a-day cap, from real token counts.

Read-only against the database it is pointed at: the notes cache-fill is patched out, so
this can be run against production content without writing to it.

    DATABASE_URL="<the production url>" JEENE_DOUBTS_MODEL=gpt-5-mini \\
      .venv/bin/python evals/doubts/run.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from dataclasses import dataclass

import asyncpg
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

from app import db, notes_text  # noqa: E402
from app.doubts import answer as answer_module  # noqa: E402
from app.doubts import context, prompt  # noqa: E402
from app.doubts.provider import build_doubt_provider  # noqa: E402
from app.providers.base import ProviderError  # noqa: E402
from evals.doubts.cases import CASES, Case  # noqa: E402

TENANT = "JEENE_MASTER"
USD_INR = 88.0
DAILY_CAP = 10

#: USD per 1M tokens: input, cached input, output.
PRICES = {
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
}


@dataclass
class Result:
    case: Case
    answered: bool
    dropped: str | None
    text: str
    citations: int
    used_notes: bool
    tokens_in: int
    tokens_out: int
    cached: int
    latency_ms: int
    format_faults: list[str]
    forbidden_hits: list[str]

    @property
    def grounded(self) -> bool:
        return self.dropped is None

    @property
    def label_ok(self) -> bool:
        return self.answered == self.case.expect_answer

    @property
    def ok(self) -> bool:
        return (self.grounded and self.label_ok
                and not self.format_faults and not self.forbidden_hits)


def maths_faults(text: str) -> list[str]:
    """The formatting failures the app cannot recover from."""
    faults = []
    if "$$" in text:
        faults.append("$$ renders as an empty span then raw source")
    if "\\begin{" in text:
        faults.append("\\begin{} renders as raw source")
    if text.count("$") % 2:
        faults.append("odd number of $ — the last span swallows the rest as prose")
    return faults


async def notes_readonly(connection, chapter_id: str, tenant: str) -> str:
    """`text_for` without its cache-filling UPDATE, so this can read production."""
    row = await connection.fetchrow(
        "SELECT pdf_url, coalesce(extracted_text, '') AS cached FROM chapter_notes "
        "WHERE chapter_id = $1 AND tenant_id = $2 AND status = 'published'",
        chapter_id, tenant,
    )
    if row is None:
        return ""
    if row["cached"]:
        return row["cached"]
    return await notes_text._extract(row["pdf_url"], chapter_id) or ""


async def run_case(conn, provider, case: Case) -> Result | None:
    anchor = await context.resolve(conn, "node", case.chapter, TENANT)
    if anchor is None:
        print(f"  !! {case.name}: chapter {case.chapter} does not resolve")
        return None

    material = await context.gather(conn, anchor, case.question, "eval", TENANT)
    if material.is_empty():
        print(f"  !! {case.name}: no material in {case.chapter}")
        return None

    # The rule enforced by omission rather than instruction: a worked example's correct
    # option is never sent, so it cannot be leaked. Checked here because it is the one
    # guarantee that does not depend on the model behaving.
    block = prompt.material_text(material)
    if block.count("Correct answer") > (1 if material.focus else 0):
        print(f"  !! {case.name}: the material block is carrying answer keys")

    started = time.monotonic()
    try:
        outcome = await answer_module.answer(provider, material, case.question)
    except ProviderError as exc:
        print(f"  !! {case.name}: provider failed ({exc})")
        return None
    elapsed = int((time.monotonic() - started) * 1000)

    usage = outcome.usage
    return Result(
        case=case,
        answered=outcome.answered,
        dropped=outcome.dropped_because,
        text=outcome.text,
        citations=len(outcome.used_concept_ids) + len(outcome.used_question_ids),
        used_notes=outcome.used_notes,
        tokens_in=usage.input_tokens if usage else 0,
        tokens_out=usage.output_tokens if usage else 0,
        cached=usage.cached_input_tokens if usage else 0,
        latency_ms=elapsed,
        format_faults=maths_faults(outcome.text),
        forbidden_hits=[f for f in case.forbidden if f.lower() in outcome.text.lower()],
    )


def report(results: list[Result], model: str) -> bool:
    print("\n" + "=" * 90)
    print(f"{'case':28} {'group':8} {'want':7} {'got':7} {'cites':>5} {'ms':>6}  verdict")
    print("-" * 90)
    for r in results:
        want = "answer" if r.case.expect_answer else "refuse"
        got = "answer" if r.answered else "refuse"
        verdict = "ok" if r.ok else "FAIL"
        print(f"{r.case.name:28} {r.case.group:8} {want:7} {got:7} "
              f"{r.citations:>5} {r.latency_ms:>6}  {verdict}")

    failures = [r for r in results if not r.ok]
    if failures:
        print("\n" + "-" * 90)
        for r in failures:
            print(f"\nFAIL {r.case.name}")
            print(f"  why this case exists: {r.case.why}")
            if not r.grounded:
                print(f"  DROPPED: {r.dropped}  <- cited material it was never given")
            if not r.label_ok:
                print(f"  wanted {'an answer' if r.case.expect_answer else 'a refusal'}, "
                      f"got {'an answer' if r.answered else 'a refusal'}")
            for fault in r.format_faults:
                print(f"  MATHS: {fault}")
            for hit in r.forbidden_hits:
                print(f"  LEAKED: {hit!r} appears in the reply")
            print(f"  said: {r.text[:300]}")

    total = len(results)
    grounded = sum(r.grounded for r in results)
    labelled = sum(r.label_ok for r in results)
    faults = sum(bool(r.format_faults) for r in results)
    wanted_answer = [r for r in results if r.case.expect_answer]
    wanted_refuse = [r for r in results if not r.case.expect_answer]

    print("\n" + "=" * 90)
    print(f"grounding   {grounded}/{total}  answers built only from material they were given")
    print(f"labels      {labelled}/{total}  "
          f"({sum(r.label_ok for r in wanted_answer)}/{len(wanted_answer)} answered, "
          f"{sum(r.label_ok for r in wanted_refuse)}/{len(wanted_refuse)} refused)")
    print(f"maths       {total - faults}/{total}  replies the app can draw")
    print(f"notes used  {sum(r.used_notes for r in results)}/{total}")

    in_rate, cached_rate, out_rate = PRICES.get(model, PRICES["gpt-5-mini"])
    total_in = sum(r.tokens_in for r in results)
    total_cached = sum(r.cached for r in results)
    total_out = sum(r.tokens_out for r in results)
    usd = ((total_in - total_cached) * in_rate
           + total_cached * cached_rate + total_out * out_rate) / 1e6
    per = usd / max(total, 1)
    print(f"\ncost        ₹{per * USD_INR:.2f}/answer   "
          f"₹{per * USD_INR * DAILY_CAP * 30:.0f}/month for a student who uses all ten daily")
    print(f"latency     {min(r.latency_ms for r in results)}–"
          f"{max(r.latency_ms for r in results)}ms")

    # The number the design document said to watch. Near zero means it is inventing;
    # very high means the material is too thin. This set is built to sit in between.
    declined = sum(1 for r in results if not r.answered)
    print(f"\nrefusal rate on this set: {declined}/{total} "
          f"({100 * declined / total:.0f}%) — {len(wanted_refuse)} of them by design")

    print("\n" + ("PASS" if not failures else f"{len(failures)} FAILED"))
    return not failures


async def main() -> int:
    model = os.environ.get("JEENE_DOUBTS_MODEL", "gpt-5-mini")
    os.environ.setdefault("JEENE_DOUBTS_ENABLED", "true")
    from app.config import settings
    settings.jeene_doubts_enabled = True
    settings.jeene_doubts_model = model

    provider = build_doubt_provider()
    if provider is None:
        print("Ask Jeene is not configured; set OPENAI_API_KEY and JEENE_DOUBTS_MODEL")
        return 2

    conn = await asyncpg.connect(os.environ["DATABASE_URL"], ssl="require")
    await db._init_connection(conn)
    context.notes_text.text_for = notes_readonly

    print(f"model={model}  cases={len(CASES)}")
    results: list[Result] = []
    try:
        for case in CASES:
            print(f"  {case.name}…", flush=True)
            result = await run_case(conn, provider, case)
            if result is not None:
                results.append(result)
    finally:
        await conn.close()

    if not results:
        print("nothing ran")
        return 2
    return 0 if report(results, model) else 1


# Guarded, because the helpers above are worth importing — and without this, importing
# one of them runs the whole paid evaluation as a side effect. Which it did.
if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
