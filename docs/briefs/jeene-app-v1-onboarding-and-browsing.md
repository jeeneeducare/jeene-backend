---
name: Feature brief
about: Product Owner's intent for the Jeene mobile app v1 — avatar onboarding + Browsing mode — plus the full product vision
title: "[Brief] Jeene app v1 — avatar onboarding + Browsing mode (with the full product vision)"
labels: ["brief"]
audience: UI team (KMP app, separate repo)
---

## 🎯 What we're building & why

Jeene is an AI exam-prep app for NEET. This brief frames the **complete product vision** so the app is structured for where we're going, then pins down the **one milestone to build now**.

**The complete vision.** When the app opens, our avatar **Jeene** greets the student and asks two quick taps — **class** (e.g. 12th) and **target exam** (e.g. NEET) — then gives a short guided tour. The app then lives in **two modes**, switchable from a **bottom bar**:

- **Browsing mode (default):** the student explores structured study material — our NCERT-grounded content, or, for a student whose login belongs to a coaching centre on our B2B plan, *that centre's* content instead. Material is organised into **sections** (NCERT questions, NCERT Exemplar, PYQs, and more) and drilled down by **chapter → topic → concept**. As the student practises, we record what they attempt and how long they spend, and turn that into **progress, statistics (time per subject), and recommendations** ("focus on friction, you missed most of its questions"), including post-test weakness analysis and suggested paths.
- **Jeene mode (AI, later):** the student writes to Jeene in plain language ("I just did Newton's Laws in school and don't get X, train me to NEET level"), and Jeene assembles a **clickable roadmap** from our content — watch this, attempt these questions, read these notes, take this test.

**Monetization.** Every student gets **3 chapters fully free**; beyond that they're prompted to subscribe in-app.

**Identity.** Students **browse freely without an account**; we ask them to log in only when they do something worth saving.

**What we build NOW.** Bring the app to life with two things: (1) **avatar-led onboarding** (class + exam tap, then into the app — *no login*), and (2) **Browsing mode** over our live content backend — chapters, the concept tree, questions with LaTeX math and figures, and a **separate** answer/solution reveal. The result is a real, demoable app a student can actually study from.

## 👤 Who it's for

NEET aspirants, largely school students (class 11–12), many **under 18**, studying on their phones. First-time users who should be able to open the app and start exploring **real study material within seconds, with no signup wall**.

## ✅ What it must do (capabilities) — the build-now scope

- Greet the student with the **Jeene avatar** and ask, in a couple of taps, their **class** and **target exam**. No login at this step.
- Drop the student into **Browsing mode by default**, with a **bottom bar** that already reserves the second slot for **Jeene mode** (shown as coming-soon / stubbed for now).
- Let the student **browse chapters**, open a chapter's **tree (topics → subtopics → concepts)**, and reach its **questions**.
- Present questions in **sections by type** — NCERT (MCQ), NCERT Exemplar, PYQ, assertion-reasoning, numerical — so the student chooses what to practise.
- Show each question faithfully: **LaTeX math rendered** (not flattened), options, difficulty, and any **figure** image.
- Reveal the **correct answer and worked solution only on an explicit tap** (a "show solution" action) — never up front. *(Recording the attempt is a later milestone; for now the reveal is read-only.)*
- Everything above works **without an account**.

## 🌟 What "good" looks like

A student opens the app, taps **class 12 → NEET**, and within seconds is reading a real NEET-level question from our bank with the **math rendered exactly like a textbook**, flipping between **NCERT / Exemplar / PYQ** sections, and revealing the worked solution when they choose to. It feels like a **real product, not a shell**, and it's obvious where the second (Jeene) mode and "save your progress" will slot in later.

## 🚫 Non-negotiables

- **Answer integrity.** The correct option and solution are never shown before the student's explicit reveal. (The backend enforces this — question fetches don't even contain the answer; the reveal is a separate call.)
- **No login before value.** Never gate onboarding or browsing behind login. The class/exam step takes no account; login is prompted only at the first save-type action (a later milestone).
- **Backend is the only data source.** The app talks only to our backend API; it never invents content or reaches a database directly.
- **Math stays faithful.** Render LaTeX as-is; never show flattened or garbled math.
- **Tenant-swappable from day one.** Build with Jeene's brand as the default, but keep theming/branding **tokenised** so a B2B tenant's look can be swapped later. Don't hardcode brand values.
- **Questions-only for now.** Notes and videos are out of this milestone; don't block browsing on them.

## 🧭 Technical steers

- `[hard]` Follow the **KMP architecture guide** (the `/kmp-arch` skill): native UI per platform (**Jetpack Compose** on Android, **SwiftUI** on iOS) over a **shared Kotlin module** (models, repository interfaces, use cases). No shared UI, no expect/actual, no DI framework, mirror feature parity across platforms.
- `[hard]` Integrate with the **live content API** at `https://jeene-backend.onrender.com`; the exact, current contract is the OpenAPI docs at **`/docs`**. Content endpoints are **public** (no auth needed for browsing).
- `[preference]` Per the kmp-arch caching pattern, **fetch a chapter's content once and filter/paginate the sections client-side** rather than a network round-trip per section — a chapter's question set fits comfortably in memory.
- `[preference]` Structure navigation so the **two-mode bottom bar** and a later **"sign in to save"** interception are natural to add, not a refactor.

## 🧊 Happy to defer

- Notes and videos as content types.
- **Jeene AI mode** (the roadmap chat) — design the bottom-bar slot, don't build the feature.
- Login / accounts, progress recording, statistics, recommendations, weakness analysis.
- Monetization (3 free chapters, subscription / paywall).
- B2B **tenant content switching** — design theming to be swappable, but don't build tenant resolution now.
- The **guided tour** can be minimal/stubbed if needed; the class/exam onboarding is the must-have.

## 📎 References

- **Live API contract:** `https://jeene-backend.onrender.com/docs` (OpenAPI). Endpoints: `GET /chapters`, `/chapters/{id}/tree`, `/chapters/{id}/questions`, `/concepts/{id}/questions`, `/questions/{id}`, `/questions/{id}/answer`.
- **Product spec:** jeene-backend `docs/PRD.md` (vision, v1 scope, answer-integrity rule, multitenancy).
- **Architecture:** the `/kmp-arch` skill (native UI + shared logic).
- **Avatar assets:** the Jeene avatar is ready; **the PO provides the avatar assets/integration to the UI terminal** (already shared with it).
- **Designs:** a mix — some **Figma provided by the PO**, some screens **designed by the UI team's Claude** against the Jeene brand kit. The UI Claude should ask the PO for the Figma/assets for any screen that has an approved design **before** building it.
- **Test content available now:** chapter `phy_11_ch4` (Laws of Motion) — 6 topics / 14 subtopics / 30 concepts, 163 questions across MCQ / Exemplar / PYQ / assertion-reasoning, some with figures.
