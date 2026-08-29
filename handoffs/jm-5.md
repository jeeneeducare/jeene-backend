**Ticket:** JM-5 — Jeene Mode: the provider port, the prompt, and validation

**Branch:** `jm-5-planner-provider` (on top of the JM-1→JM-4 stack and its database verification pass)

**Summary:**
The model enters, behind a flag that ships off, with the deterministic planner already load-bearing underneath it. `app/providers/` holds a protocol and one implementation; `app/plans/prompt.py` holds a static, versioned system prompt; `app/plans/validate.py` holds the ladder that decides whether a produced plan may be shown to a student; `app/plans/generate.py` orchestrates generate → repair once → fall back. `_generate` in the router — the seam JM-4 left — is now three lines and still names no provider.

Ten plans were generated against the live API and read alongside the deterministic ones. **All ten passed validation on the first attempt**, with no repair round-trip and no fallback, at a mean of 17.5 seconds and 56% of input tokens served from cache. The review pack is `handoffs/jm-5-review-pack.md` and is the part that still needs a teacher.

**Files changed:**
- `app/providers/base.py`, `app/providers/openai_provider.py` (new) — the protocol, `ProviderUsage`, `ProviderError`, and the OpenAI implementation using `chat.completions.parse` with a Pydantic response format. Timeout 45s, **no SDK retries** — a retry doubles the wait for a student watching a spinner, and the repair and the fallback are both faster.
- `app/plans/prompt.py` (new) — `SYSTEM` and `PROMPT_VERSION` (now 3). A plain literal, never an f-string: providers cache long identical prefixes and one varying byte destroys that.
- `app/plans/validate.py` (new) — the ladder. Repairable problems are fixed in place and recorded; fatal ones go back once, named specifically.
- `app/plans/generate.py` (new) — the three rungs, and the log line.
- `app/plans/schema.py` — `min_steps` 4 → 3, `max_steps` 8 → 6.
- `app/plans/inventory.py` — `min_steps` is now computed per scope.
- `app/routers/plans.py` — `_generate` calls the ladder; the provider is built once and cached.
- `app/config.py`, `.env.example`, `requirements.txt` — three settings by name, `openai==3.6.0`.
- `db/testdata/seed_plans.sql` — four variants per bucket (see below).
- `tests/conftest.py` (new) — no test calls a provider unless it says so.
- `tests/test_plans_validate.py` (new, 58 tests), `tests/test_plans_generate.py` (new, 19 tests).

**How to test:**
1. `.venv/bin/python -m pytest tests/ -q` → `289 passed, 11 skipped` with a local database; `256 passed, 44 skipped` without one. **No test makes a network call**, with or without a key configured.
2. To exercise the real provider, set `OPENAI_API_KEY`, `JEENE_PLANNER_MODEL=gpt-5.2` and `JEENE_PLANNER_ENABLED=true` in `.env`, then create a plan through the API and check the log line reports `origin=model`.
3. Unset the key and create another → the plan still arrives, `origin=fallback`, no error.
4. Read `handoffs/jm-5-review-pack.md`.

**Acceptance criteria:**
- [x] `PlannerProvider` protocol plus the OpenAI implementation using the official SDK and strict structured outputs.
- [x] Model id and enablement come from configuration; a missing key means fallback, never a 500 — asserted four ways in `test_every_path_returns_a_plan`.
- [x] Every validation rung has a test with a hand-written bad plan — 58 of them.
- [x] One repair round-trip carries the specific failures; a second failure falls back.
- [x] Every generation logs origin, model, tokens, latency and failures — and never prompt or response text. A test asserts the scope *title* never reaches the log while the scope *id* does.
- [x] `.env.example` documents the three variables by name with no values.
- [~] **A ten-inventory review pack, read alongside the deterministic ones** — the pack is written and attached. **It has not been read by the teacher.** That reading is the acceptance criterion, not the generating, and it is the same open item as JM-2's guidance strings.

**Defects found and fixed, all in code written in this ticket:**

1. **The validator rejected the deterministic planner's own output.** Groundwork sits outside the scope, so it has no buckets — only a count on the candidate — and `_available()` looked only at buckets. Every foundation step ever written was rejected as asking for questions that do not exist, including the fallback's. Found by running validation over the fallback's output, which is now a parametrised test across twelve configurations.
2. **A selector naming a subtopic was rejected.** `resolve.py` expands any node to its concept descendants and the schema says so, but the validator compared selector ids to bucket keys directly. Correct plans were being thrown away.
3. **`min_steps` was 4 while the deterministic planner deliberately produces fewer for a thin scope.** The model was being told to write four steps for a scope that supports two, so it padded — inventing work, which is exactly what JM-2 decided against. `min_steps` is now computed from what the scope actually holds.
4. **A unit test made a real, paid API call.** A test asserting "the deterministic planner ran" spent fifteen seconds proving the opposite, because `.env` happened to be configured. `tests/conftest.py` now disables the provider for every test that has not opted in. Money is the smaller half: a suite that reaches the network fails on a train and cannot be trusted to say what broke.
5. **Generated prose used the data model's vocabulary** — "open the node", "work through this bucket". A student has never heard those words. Now a fatal rung, and a prompt rule.
6. **A step of nothing but questions was labelled `learn`** — it reads as a lesson and opens as a quiz. Relabelled in place rather than rejected; the step was good work wearing the wrong name.

**The fallback rate, and why the first number was wrong:**
The first ten-case run fell back **6 times out of 10**. Almost all of it was the fixture: the seed had exactly one question per (concept × type × difficulty), so any filter naming two facets matched one or two questions and tripped the floor. A fixture thin enough to make correct plans look wrong is measuring the fixture. With four variants per bucket, defects 1–3 fixed and three prompt gaps closed, the same ten cases now go **10/10 with no repair**. That number is from a synthetic seed and should be re-measured on a real chapter before the flag goes on anywhere.

**Cost, measured rather than estimated:**
Across ten plans: 37,085 input tokens, 13,207 output, 20,992 cached (56%). Roughly 3.7k in and 1.3k out per plan, one call each. Mean latency 17.5s, range 13–23s — which is a real wait, and the mentor animation JM-7 specifies is not optional.

**Decisions taken in this ticket:**
- **`max_steps` lowered to 6** to match the deterministic planner, so a plan's length does not visibly depend on which planner produced it.
- **An unfillable filter is dropped when its step keeps another**, and only fatal when it is the step's last item. Rejecting a six-step plan over one thin sub-filter spends a round trip, and then a student's patience, on something the validator can correct exactly.
- **A graded step wrongly marked self-markable is coerced to graded, never the reverse.** Getting this wrong in the lenient direction makes the whole plan tappable, which is the one thing the feature cannot survive.

**What is still unverified:**
- **The pack has not been read by a teacher.** Nothing here says the plans are good, only that they are valid and that they look better than the template to a developer.
- **Only one model, one seed, ten cases.** No measurement on real chapter content, and no second opinion on `gpt-5.2` versus anything cheaper.
- **Prompt injection is untested against real content.** The rule is in the prompt and the structural guarantee is in the validator, but no seeded node title has actually tried to talk the planner out of its instructions.

**Notes for whoever turns the flag on:**
- Ship `JEENE_PLANNER_ENABLED=false`. The feature is complete without it.
- Watch `origin=fallback` rate first, then `cached=` — if caching drops to zero on a warm route, something has started varying in the static half of the prompt.
- `PROMPT_VERSION` is 3 and is written to every plan. Bump it when `SYSTEM` changes in a way that would produce different plans.
- The key is set in the local gitignored `.env` only. It appears in no committed file, no log line and no handoff. **It was pasted into a chat transcript, so rotate it before it is used for anything that matters.**
