# JM-5 review pack — ten generated plans, beside the deterministic ones

**For the teacher.** Whether these are *valid* is settled: the validator checks it and all
ten passed on the first attempt, with no repair round-trip and no fallback. The question
here is different, and it is the only one that decides whether the flag should ever go on:

> **Is the generated plan better than the deterministic one printed beneath it?**

If it is not, the call is not worth making. The template costs nothing, never fails, and
never invents.

Worth looking for specifically:

- Does the model **notice** anything the template cannot? A weak concept handled
  differently from a strong one, the scope's own description put to use, an order that
  suits this student rather than all students.
- Are the `how_to_use` lines **instructions** a student could follow without thinking, or
  are they encouragement dressed up?
- Does anything read as **teaching**? Nothing here has ever seen a question, so any
  sentence about the physics is invented and should be reported as a bug, not a wording
  preference.
- Is the **shape** right — difficulty climbing, groundwork only where the record justifies
  it, a checkpoint that measures something?

No question text, options, answers or solutions appear anywhere below. A plan cannot carry
them: the planner was never sent any.

**Method.** Ten scopes across two chapters, three proficiencies, three intents, and two
students — one with a record that includes a real prerequisite gap, one with none. Model
`gpt-5.2`, prompt version 3, against the local seed in `db/testdata/`.

---

## Acceleration due to gravity — intermediate, first_time

`phy_11_ch8_s1` · student: has a record · scope holds 144 questions, 1 video(s), 1 note set(s) · 1 groundwork candidate(s)

Generated in 23318 ms — 4063 in / 1637 out / 1664 cached.

### Generated

> You’ve attempted very little of this subtopic so far, and most of your mistakes are coming from Variation of g with depth; we’ll fix the base, then build up with easy→medium practice, and finish with a fresh medium checkpoint across height/depth/latitude.

**1. [practise] · **groundwork** Foundation fix: Newton’s second law (only what you’re getting wrong)**  
*Your low accuracy here is likely leaking into gravitation question setups, so tightening this will make the next practice faster and cleaner.*

- Do 6 questions in one sitting, mixed order, without hints.
- After the 6, review only the wrong ones and write a 1-line note for each: what you misread / which step you missed.
- Do 6 more questions, but avoid repeating the exact same approach that caused the earlier mistakes (use your 1-line notes).
- `12 questions, mcq, easy, mixed`
- *done when: accuracy — 12 answered at 60%*

**2. [learn] Learn pass: Variation of g (video + quick notes skim)**  
*You’re starting this subtopic and need a single clean pass before you ramp up question volume.*

- Watch the video end-to-end without pausing; note down only headings (height / depth / latitude) and 2–3 bullet cues under each.
- Then open the chapter notes and quickly locate the same parts; add 1 missing bullet under each heading.
- Stop there—don’t try to memorise; you’ll lock it in through questions next.
- `video: aaaaaaaaaaa`
- `notes: phy_11_ch8`
- *done when: self — student marks it*

**3. [practise] Depth (easy): rebuild confidence and consistency**  
*Depth is your current weak concept, so we’ll stabilise it at easy level before moving up.*

- Attempt 5 easy MCQs on depth in 2 mini-sets (3 then 2).
- After each mini-set, check solutions and write a 1-line reason for every mistake.
- Re-attempt only the wrong ones once immediately (same step) to confirm the fix.
- `5 questions, mcq, easy, easiest_first`
- *done when: accuracy — 5 answered at 80%*

**4. [practise] Depth (medium): convert the fix into NEET-level accuracy**  
*You already dropped points on medium/hard depth questions, so medium is the next step toward your target level.*

- Do 4 medium MCQs on depth in one go, timed (max ~2 min per question).
- Check solutions only after all 4; for each wrong one, tag the mistake as: concept miss / calculation slip / option trap.
- Immediately do 4 medium NCERT Exemplar on depth to reinforce the same idea in a different style.
- `4 questions, mcq, medium, mixed`
- `4 questions, ncert_exemplar, medium, mixed`
- *done when: accuracy — 8 answered at 60%*

**5. [practise] Height + Latitude (easy→medium): build coverage quickly**  
*Your coverage on height/latitude is almost zero, so you need quick breadth before the final checkpoint.*

- Do 4 easy MCQs on height, then 4 easy MCQs on latitude; check after each set of 4.
- Then do 4 medium MCQs (mixed: height + latitude).
- For every wrong answer, open the chapter notes and find the exact spot that resolves it; add a 1-line fix to your bullets.
- `4 questions, mcq, easy, easiest_first`
- `4 questions, mcq, easy, easiest_first`
- `4 questions, mcq, medium, mixed`
- `notes: phy_11_ch8`
- *done when: accuracy — 12 answered at 60%*

**6. [verify] Checkpoint (medium, fresh): Acceleration due to gravity (full subtopic)**  
*This confirms you can now score at the target difficulty across depth/height/latitude without repeats.*

- Attempt all questions in one timed sitting.
- Do not open notes/video during the attempt.
- After finishing, review every wrong question and write a 1-line takeaway; if accuracy is below target, repeat Step 4 or Step 5 based on which concept dominates your errors.
- `12 questions, mcq/ncert_exemplar/pyq, medium, mixed, unseen only`
- *done when: checkpoint — 12 answered at 60%*

### Deterministic, for comparison

> 6 steps through Acceleration due to gravity, about 1h 42m of work.

**1. [practise] · **groundwork** Shore up Newton's second law**  
*Acceleration due to gravity leans on this from Laws of Motion, and your answers there are coming out wrong more often than not.*

- This is groundwork from an earlier chapter, not the topic you asked about.
- Do these before anything else — the rest of the plan assumes them.
- If you get most of them right quickly, mark the step done and move on.
- `5 questions, mcq/ncert_exemplar/pyq, easy/medium, easiest_first`
- *done when: accuracy — 5 answered at 60%*

**2. [learn] Get the picture first**  
*A pass over the whole idea before you start answering, so the questions are recognition rather than guesswork.*

- Go through it once without taking notes — you are building a picture, not a record.
- Then go back and write down only the parts that surprised you.
- If a derivation loses you, mark where and keep going; the practice below will tell you whether it mattered.
- `video: aaaaaaaaaaa`
- *done when: self — student marks it*

**3. [practise] Find your feet**  
*A gentle set across the whole of Acceleration due to gravity, to show you where you already stand.*

- Do the first three without looking anything up, even if you are stuck.
- Check all three at once rather than one at a time — a pattern of mistakes tells you more than a single one.
- For anything you got wrong, open Understand with AI before starting the next batch.
- `8 questions, mcq/ncert_exemplar/pyq, easy/unrated, easiest_first`
- *done when: accuracy — 6 answered at 60%*

**4. [practise] Go at what is not sticking**  
*Variation of g with depth accounts for more of your wrong answers here than anything else.*

- These are the concepts your answers say are shakiest, not the ones that look hardest.
- Go slowly. Speed here just repeats the same mistake faster.
- Read the solution for every one you get wrong, including the ones you nearly had.
- `8 questions, mcq/ncert_exemplar/pyq, medium, mixed`
- *done when: accuracy — 6 answered at 60%*

**5. [practise] See how it is actually asked**  
*Past questions combine ideas rather than testing one, which is the part practice on its own never prepares you for.*

- These are real exam questions, so expect them to combine ideas rather than test one.
- Before answering, name which concept the question is really about; that habit is worth more than the answer.
- `10 questions, pyq, medium, mixed`
- *done when: accuracy — 7 answered at 60%*

**6. [verify] Check you have Acceleration due to gravity**  
*Questions you have not seen, at the level you are aiming for. This is what finishing the plan means.*

- Sit this in one go, without notes, and without checking anything as you answer.
- Nothing is revealed until you have answered every question — that is deliberate, and it is the only way this tells you anything true.
- If you miss a few, the plan adds work on exactly those concepts. You do not start over.
- `10 questions, mcq/ncert_exemplar/pyq, medium, mixed, unseen only`
- *done when: checkpoint — 8 answered at 70%*

---

## Acceleration due to gravity — advanced, exam_soon

`phy_11_ch8_s1` · student: has a record · scope holds 144 questions, 1 video(s), 1 note set(s) · 1 groundwork candidate(s)

Generated in 16992 ms — 4063 in / 1421 out / 3840 cached.

### Generated

> You’ve tried very few questions overall in this subtopic, and most errors are coming from “Variation of g with depth.” We’ll quickly patch the Newton’s second law gap, then build depth → height/latitude coverage, and finish with a hard mixed checkpoint (all unseen).

**1. [practise] · **groundwork** Foundation fix: Newton’s second law (quick accuracy bump)**  
*Your low accuracy on this prerequisite is likely causing avoidable mistakes when you solve g-variation questions.*

- Do 6 questions only, in mixed order, without any notes/video open.
- After attempting all 6, check solutions together and write a 2–3 word reason for each wrong one (e.g., “wrong sign”, “wrong setup”, “rushed reading”).
- If you get <4/6 correct, stop and immediately redo the same 6 once (still without notes) to lock the fix.
- `6 questions, mcq, easy, mixed`
- *done when: accuracy — 6 answered at 67%*

**2. [learn] One-pass learning: Acceleration due to gravity (whole subtopic)**  
*You have low coverage across height/latitude and need a clean, single reference pass before you scale question volume.*

- Watch the video once end-to-end and pause only to note headings/keywords you keep forgetting.
- Immediately after the video, skim the Gravitation chapter notes and mark the exact sections that match: height, depth, latitude (just bookmarks, no rewriting).
- Make a 10-line “error-prevention” list: 1 line each for common traps you personally make (based on Step 1 and your depth errors).
- `video: aaaaaaaaaaa`
- `notes: phy_11_ch8`
- *done when: self — student marks it*

**3. [practise] Depth (your weak area): rebuild from easy → medium**  
*Most of your attempts are in depth and accuracy is low, so we’ll stabilise it before touching hard.*

- Do 5 easy MCQs first (don’t mix difficulties).
- Then do 4 medium MCQs.
- Review all 9 together: for every wrong, open explanation (when available) and add one line to your error-prevention list; then re-attempt that question once immediately.
- `5 questions, mcq, easy, easiest_first`
- `4 questions, mcq, medium, easiest_first`
- *done when: accuracy — 9 answered at 67%*

**4. [practise] Coverage boost: Height + Latitude (easy first, then one PYQ set)**  
*You’ve barely touched height/latitude, so you need fast breadth to avoid surprises in the checkpoint.*

- Do 4 easy MCQs from height and 4 easy MCQs from latitude (8 total).
- Then do 4 easy PYQs mixed between height/latitude (don’t look up anything mid-way).
- After checking, tag your top 3 recurring mistake patterns in your error-prevention list and reattempt only those wrong ones once.
- `4 questions, mcq, easy, mixed`
- `4 questions, mcq, easy, mixed`
- `4 questions, pyq, easy, mixed`
- *done when: accuracy — 12 answered at 67%*

**5. [verify] Checkpoint (hard, unseen): Acceleration due to gravity**  
*This confirms you can handle exam-level hard questions across depth/height/latitude without repeats.*

- Attempt all questions in one sitting with exam timing; no notes/video.
- After finishing, review solutions and write a 1-line cause for each wrong (concept gap vs. setup vs. calculation vs. reading).
- If you miss the accuracy target, redo only the wrong questions after 24 hours (not immediately).
- `12 questions, mcq/ncert_exemplar/pyq, hard, mixed, unseen only`
- *done when: checkpoint — 12 answered at 50%*

### Deterministic, for comparison

> 6 steps through Acceleration due to gravity, about 1h 42m of work.

**1. [practise] · **groundwork** Shore up Newton's second law**  
*Acceleration due to gravity leans on this from Laws of Motion, and your answers there are coming out wrong more often than not.*

- Your record says this earlier idea is shaky, and it sits underneath the topic.
- Give it one pass. If it holds up, skip straight to the practice below.
- `5 questions, mcq/ncert_exemplar/pyq, easy/medium, easiest_first`
- *done when: accuracy — 5 answered at 60%*

**2. [learn] Get the picture first**  
*A pass over the whole idea before you start answering, so the questions are recognition rather than guesswork.*

- Do not go through this end to end. Use it only for the concepts named on this step.
- Give it fifteen minutes at most — this close to an exam, questions teach faster than explanations.
- `video: aaaaaaaaaaa`
- *done when: self — student marks it*

**3. [practise] Find your feet**  
*A gentle set across the whole of Acceleration due to gravity, to show you where you already stand.*

- Answer every one, including the ones you would normally skip. A guess still shows you where the gap is.
- Check the whole set in one go and write down only the concepts you missed.
- `8 questions, mcq/ncert_exemplar/pyq, easy/unrated, easiest_first`
- *done when: accuracy — 6 answered at 60%*

**4. [practise] Go at what is not sticking**  
*Variation of g with depth accounts for more of your wrong answers here than anything else.*

- These are the cheapest marks you can still buy: concepts you have met and are getting wrong.
- Do them all, then re-do the ones you missed immediately rather than later.
- `8 questions, mcq/ncert_exemplar/pyq, hard, mixed`
- *done when: accuracy — 6 answered at 60%*

**5. [practise] See how it is actually asked**  
*Past questions combine ideas rather than testing one, which is the part practice on its own never prepares you for.*

- Sit these like the real thing: one sitting, no notes, no checking as you go.
- Afterwards, sort your mistakes into 'did not know' and 'knew but slipped'. They need different fixes.
- `10 questions, pyq, hard, mixed`
- *done when: accuracy — 7 answered at 60%*

**6. [verify] Check you have Acceleration due to gravity**  
*Questions you have not seen, at the level you are aiming for. This is what finishing the plan means.*

- Treat this as the exam: one sitting, timed by you, nothing open beside it.
- Answer everything, including guesses, exactly as you would on the day.
- What you miss here is what to spend your remaining time on.
- `10 questions, mcq/ncert_exemplar/pyq, hard, mixed, unseen only`
- *done when: checkpoint — 8 answered at 70%*

---

## Acceleration due to gravity — basic, first_time

`phy_11_ch8_s1` · student: no record · scope holds 144 questions, 1 video(s), 1 note set(s) · 1 groundwork candidate(s)

Generated in 17029 ms — 4027 in / 1332 out / 1664 cached.

### Generated

> First build a clean first-time understanding with the video + chapter notes, then do easy questions concept-wise (height/depth/latitude), and finish with an easy mixed checkpoint over the full subtopic (new questions only).

**1. [learn] Learn: Variation of g (full subtopic pass)**  
*You’re starting this subtopic for the first time, so get one complete pass before attempting questions.*

- Watch the video once end-to-end without pausing.
- Rewatch only the parts where you felt unsure, and mark 3–5 timestamps/topics to revisit later.
- Immediately open the chapter notes and skim only the parts that match this subtopic; add 6–10 bullet points to your short notes (one line each).
- `video: aaaaaaaaaaa`
- `notes: phy_11_ch8`
- *done when: self — student marks it*

**2. [practise] Practice (Easy MCQs): height + depth**  
*Easy MCQs will quickly tell you whether your first pass is working for the two most common variations.*

- Do 4 easy MCQs from height first, then 5 easy MCQs from depth (no switching in between).
- After each set, check solutions; for every wrong one, write a 1-line mistake tag (concept slip / calculation slip / misread).
- Redo only the wrong questions once immediately (without looking), then move on.
- `4 questions, mcq, easy, easiest_first`
- `5 questions, mcq, easy, easiest_first`
- *done when: accuracy — 9 answered at 70%*

**3. [practise] Practice (Easy NCERT Exemplar): latitude + reinforcement**  
*NCERT Exemplar-style questions will firm up basics and expose small gaps before PYQs.*

- Do 4 easy NCERT Exemplar questions from latitude in one go, then check solutions together.
- Then do 4 easy NCERT Exemplar questions from height OR depth (pick the one you felt weaker in Step 2).
- Log only the questions you got wrong/guessed into a quick ‘redo list’ (just the attempt IDs in the app).
- `4 questions, ncert_exemplar, easy, mixed`
- `4 questions, ncert_exemplar, easy, mixed`
- *done when: accuracy — 8 answered at 70%*

**4. [practise] Practice (Easy PYQs): mixed across all three**  
*A small set of easy PYQs will help you adapt your basics to exam-style framing.*

- Do 6 easy PYQs in mixed order across height, depth, and latitude.
- After finishing all 6, review solutions; for each wrong one, reopen the chapter notes and find the exact line/section it relates to.
- Redo the same 6 PYQs once (immediately) aiming for 100% on the second pass.
- `6 questions, pyq, easy, mixed`
- `notes: phy_11_ch8`
- *done when: accuracy — 6 answered at 67%*

**5. [verify] Checkpoint (Easy, new questions): Acceleration due to gravity**  
*This checks if you can score reliably on this subtopic at the target easy level without repeats.*

- Attempt all questions in one sitting, timed (aim ~1–1.5 min per question).
- Don’t open notes/video during the attempt.
- After submission, list the concepts of every wrong/guessed question (height/depth/latitude) to decide what to revise next.
- `12 questions, mcq/ncert_exemplar/pyq, easy, mixed, unseen only`
- *done when: checkpoint — 12 answered at 60%*

### Deterministic, for comparison

> 5 steps through Acceleration due to gravity, about 1h 26m of work.

**1. [learn] Get the picture first**  
*A pass over the whole idea before you start answering, so the questions are recognition rather than guesswork.*

- Go through it once without taking notes — you are building a picture, not a record.
- Then go back and write down only the parts that surprised you.
- If a derivation loses you, mark where and keep going; the practice below will tell you whether it mattered.
- `video: aaaaaaaaaaa`
- *done when: self — student marks it*

**2. [practise] Find your feet**  
*A gentle set across the whole of Acceleration due to gravity, to show you where you already stand.*

- Do the first three without looking anything up, even if you are stuck.
- Check all three at once rather than one at a time — a pattern of mistakes tells you more than a single one.
- For anything you got wrong, open Understand with AI before starting the next batch.
- `8 questions, mcq/ncert_exemplar/pyq, easy/unrated, easiest_first`
- *done when: accuracy — 6 answered at 60%*

**3. [practise] See how it is actually asked**  
*Past questions combine ideas rather than testing one, which is the part practice on its own never prepares you for.*

- These are real exam questions, so expect them to combine ideas rather than test one.
- Before answering, name which concept the question is really about; that habit is worth more than the answer.
- `10 questions, pyq, easy, mixed`
- *done when: accuracy — 7 answered at 60%*

**4. [consolidate] Tidy it up before the check**  
*A short pass over what you have been getting wrong, before it is measured.*

- Go back over the material with your wrong answers next to you.
- For each mistake, find the line that would have prevented it — that is what to remember.
- `notes: phy_11_ch8`
- *done when: self — student marks it*

**5. [verify] Check you have Acceleration due to gravity**  
*Questions you have not seen, at the level you are aiming for. This is what finishing the plan means.*

- Sit this in one go, without notes, and without checking anything as you answer.
- Nothing is revealed until you have answered every question — that is deliberate, and it is the only way this tells you anything true.
- If you miss a few, the plan adds work on exactly those concepts. You do not start over.
- `10 questions, mcq/ncert_exemplar/pyq, easy, mixed, unseen only`
- *done when: checkpoint — 8 answered at 70%*

---

## Acceleration due to gravity — intermediate, revising

`phy_11_ch8_s1` · student: no record · scope holds 144 questions, 1 video(s), 1 note set(s) · 1 groundwork candidate(s)

Generated in 17060 ms — 4029 in / 1340 out / 3840 cached.

### Generated

> Revise the full subtopic once, then build accuracy through easy→medium mixed practice across height/depth/latitude, and finish with a fresh medium checkpoint.

**1. [learn] One-pass revision: Gravitation notes + subtopic video**  
*You’re revising and have no attempt history here, so start by reloading the exact pages and the one video tied to this subtopic.*

- Open the notes and skim only the parts relevant to “Acceleration due to gravity” (don’t try to read the whole chapter).
- While reading, make a 6–8 line ‘quick sheet’ in your own words: 2 lines each for height, depth, and latitude (just headings + key takeaways).
- Watch the video once at normal speed; pause only to add/adjust your quick sheet.
- Mark 3 ‘confusing points’ (max) on your quick sheet to watch for during questions.
- `notes: phy_11_ch8`
- `video: aaaaaaaaaaa`
- *done when: self — student marks it*

**2. [practise] Easy accuracy builder (MCQs) — all 3 concepts**  
*Easy MCQs will quickly confirm your base revision across height/depth/latitude before moving up.*

- Do 10 questions in one sitting, mixed order; don’t peek at notes/video while solving.
- After every 5 questions, check and tag each wrong one as: concept-mixup / careless / stuck.
- For each wrong question, re-attempt immediately once (no guessing); then move on.
- Update your quick sheet with 1 bullet per mistake-pattern (max 5 bullets).
- `10 questions, mcq, easy, mixed`
- *done when: accuracy — 10 answered at 70%*

**3. [practise] Medium mixed set — MCQ + PYQ (focus: application)**  
*Medium-level mixed practice will push you to the NEET-ready level at your target difficulty.*

- Do 12 questions: first 6 without checking, then review all 6 at once; repeat for the next 6.
- Maintain a ‘repeat list’ of question numbers you got wrong or took too long (just write them down).
- For each wrong one, open explanation if available; then re-attempt once immediately.
- If one concept feels repeatedly weak, note it at the top of your quick sheet for final review.
- `12 questions, mcq/pyq, medium, mixed`
- *done when: accuracy — 12 answered at 65%*

**4. [consolidate] Patch-up using notes + your repeat list, then 6 NCERT Exemplar (easy)**  
*This closes gaps from your mistakes and stabilizes the basics before the final checkpoint.*

- Re-open the Gravitation notes and read only the sections needed to fix the items on your repeat list (time-box to 15 minutes).
- Rewrite your quick sheet cleanly into a final 10–12 line version (keep it super short).
- Do 6 NCERT Exemplar easy questions (mixed concepts) without looking at the sheet.
- Check answers together at the end; add only 2 final bullets to the sheet (max).
- `notes: phy_11_ch8`
- `6 questions, ncert_exemplar, easy, mixed`
- *done when: accuracy — 6 answered at 67%*

**5. [verify] Checkpoint (fresh questions) — medium, full subtopic**  
*This measures your true readiness at the target difficulty using unseen questions from the whole scope.*

- Attempt all questions in one go under exam-like timing; no notes/video.
- Don’t review mid-way; submit the whole set, then review once at the end.
- Write down only the concepts of any misses (height/depth/latitude) for what to revise next.
- `15 questions, mcq/ncert_exemplar/pyq, medium, mixed, unseen only`
- *done when: checkpoint — 15 answered at 65%*

### Deterministic, for comparison

> 5 steps through Acceleration due to gravity, about 1h 26m of work.

**1. [learn] Get the picture first**  
*A pass over the whole idea before you start answering, so the questions are recognition rather than guesswork.*

- Skim rather than study. You are finding what has faded, not learning it again.
- Anything you cannot say out loud in one sentence, note it down and let the practice below settle it.
- `video: aaaaaaaaaaa`
- *done when: self — student marks it*

**2. [practise] Find your feet**  
*A gentle set across the whole of Acceleration due to gravity, to show you where you already stand.*

- Work in batches of four and check each batch together.
- If a question takes more than two minutes, that is not revision, it is relearning — mark it and come back.
- `8 questions, mcq/ncert_exemplar/pyq, easy/unrated, easiest_first`
- *done when: accuracy — 6 answered at 60%*

**3. [practise] See how it is actually asked**  
*Past questions combine ideas rather than testing one, which is the part practice on its own never prepares you for.*

- Give yourself roughly a minute a question and stick to it.
- Check at the end, not as you go — stopping to check mid-set hides how you would actually perform.
- `10 questions, pyq, medium, mixed`
- *done when: accuracy — 7 answered at 60%*

**4. [consolidate] Tidy it up before the check**  
*A short pass over what you have been getting wrong, before it is measured.*

- Re-read only the parts covering what you missed above.
- Close it and write the key relationships from memory before you look again.
- `notes: phy_11_ch8`
- *done when: self — student marks it*

**5. [verify] Check you have Acceleration due to gravity**  
*Questions you have not seen, at the level you are aiming for. This is what finishing the plan means.*

- One sitting, no notes, no checking as you go.
- This is the measurement the whole plan has been building to, so do not look anything up to protect the score.
- Whatever you miss becomes the next steps rather than a mark against you.
- `10 questions, mcq/ncert_exemplar/pyq, medium, mixed, unseen only`
- *done when: checkpoint — 8 answered at 70%*

---

## Gravitation — intermediate, revising

`phy_11_ch8` · student: has a record · scope holds 144 questions, 2 video(s), 1 note set(s) · 1 groundwork candidate(s)

Generated in 21426 ms — 4112 in / 1555 out / 1664 cached.

### Generated

> You’ve attempted very little of Gravitation so far, and your biggest leak is medium/hard questions on “Variation of g with depth”. We’ll do a quick foundation fix (Newton’s 2nd law), then rebuild confidence on depth with easy→medium, then cover height + latitude, and finish with a fresh medium checkpoint across all three concepts.

**1. [practise] · **groundwork** Foundation refresh: Newton’s second law (only what you need for this chapter)**  
*Your accuracy on Newton’s second law is low, and it’s a prerequisite for “Variation of g with height,” so tightening this will reduce silly mistakes in the Gravitation questions.*

- Attempt 12 questions first without opening any notes/videos; keep working time tight per question.
- After 12, review all solutions in one go and write a 3-bullet “mistake log” (what you did, why it went wrong, what you’ll do next time).
- Attempt the next 12 questions and try to apply your mistake-log fixes immediately.
- `24 questions, mcq, easy, easiest_first`
- *done when: self — student marks it*

**2. [practise] Depth: rebuild confidence with easy MCQs (finish the easy pool)**  
*You’re getting many depth questions wrong; locking the easy set first will stabilize your approach before you go back to medium/hard.*

- Do all remaining easy MCQs for “Variation of g with depth” in one sitting.
- For every wrong question, immediately re-attempt it once (without looking) after reading the explanation (if shown) / solution.
- Mark any question you got wrong twice to revisit in Step 3.
- `1 questions, mcq, easy, easiest_first`
- *done when: accuracy — 1 answered at 75%*

**3. [learn] Targeted revision: Acceleration due to gravity (video + quick notes scan)**  
*Before you retry medium/hard depth and start height/latitude, a quick structured pass will reduce repeated concept-level errors.*

- Watch the video once at normal speed; pause only to note down headings + 5–8 trigger points you tend to forget.
- Immediately after the video, skim the chapter notes and add/modify your trigger points (don’t rewrite full notes).
- End by writing a 6-item “must-check list” you will mentally run on every g-variation question.
- `video: aaaaaaaaaaa`
- `notes: phy_11_ch8`
- *done when: self — student marks it*

**4. [practise] Depth: medium MCQs (new attempts only)**  
*Your current depth accuracy is low because medium/hard are breaking; medium is the next best place to fix it before you touch hard.*

- Attempt 4 medium MCQs (mixed order) without hints; keep a strict rough-work structure.
- After each question, tag it as: calculation-slip / concept-slip / misread; keep counts.
- Re-attempt only the ones you got wrong at the end of the set (without seeing the solution again).
- `4 questions, mcq, medium, mixed, unseen only`
- *done when: accuracy — 4 answered at 50%*

**5. [practise] Height + Latitude: start coverage with easy → medium (MCQs)**  
*You have near-zero coverage on height and latitude, so you need quick breadth practice to be ready for a full-chapter checkpoint.*

- Do 4 easy MCQs: 2 from height + 2 from latitude (mixed). Check together after all 4.
- Then do 4 medium MCQs: 2 from height + 2 from latitude (mixed).
- Write a one-line takeaway per wrong question (what you’ll check next time) and move on—no deep re-study here.
- `4 questions, mcq, easy, mixed, unseen only`
- `4 questions, mcq, medium, mixed, unseen only`
- *done when: accuracy — 8 answered at 50%*

**6. [verify] Checkpoint (medium): Gravitation — g variation (depth + height + latitude)**  
*This verifies you can now solve unseen medium questions across the whole scope, not just the ones you’ve already practiced.*

- Attempt all questions in one go like a mini-test (no notes/video).
- After submission, review only the wrong ones and write a 5-item list of the most common error patterns.
- If you score below target, redo Step 4 (depth medium) once more tomorrow with exclude_seen=true again.
- `12 questions, mcq/ncert_exemplar/pyq, medium, mixed, unseen only`
- *done when: checkpoint — 12 answered at 60%*

### Deterministic, for comparison

> 6 steps through Gravitation, about 1h 42m of work.

**1. [practise] · **groundwork** Shore up Newton's second law**  
*Gravitation leans on this from Laws of Motion, and your answers there are coming out wrong more often than not.*

- A quick check on something the topic below leans on.
- Five minutes here saves you guessing at which half of a later question went wrong.
- `5 questions, mcq/ncert_exemplar/pyq, easy/medium, easiest_first`
- *done when: accuracy — 5 answered at 60%*

**2. [learn] Get the picture first**  
*A pass over the whole idea before you start answering, so the questions are recognition rather than guesswork.*

- Skim rather than study. You are finding what has faded, not learning it again.
- Anything you cannot say out loud in one sentence, note it down and let the practice below settle it.
- `video: bbbbbbbbbbb`
- *done when: self — student marks it*

**3. [practise] Find your feet**  
*A gentle set across the whole of Gravitation, to show you where you already stand.*

- Work in batches of four and check each batch together.
- If a question takes more than two minutes, that is not revision, it is relearning — mark it and come back.
- `8 questions, mcq/ncert_exemplar/pyq, easy/unrated, easiest_first`
- *done when: accuracy — 6 answered at 60%*

**4. [practise] Go at what is not sticking**  
*Variation of g with depth accounts for more of your wrong answers here than anything else.*

- Straight to the concepts you have been missing — there is no warm-up in this step.
- After each wrong answer, say out loud what you would do differently before moving on.
- `8 questions, mcq/ncert_exemplar/pyq, medium, mixed`
- *done when: accuracy — 6 answered at 60%*

**5. [practise] See how it is actually asked**  
*Past questions combine ideas rather than testing one, which is the part practice on its own never prepares you for.*

- Give yourself roughly a minute a question and stick to it.
- Check at the end, not as you go — stopping to check mid-set hides how you would actually perform.
- `10 questions, pyq, medium, mixed`
- *done when: accuracy — 7 answered at 60%*

**6. [verify] Check you have Gravitation**  
*Questions you have not seen, at the level you are aiming for. This is what finishing the plan means.*

- One sitting, no notes, no checking as you go.
- This is the measurement the whole plan has been building to, so do not look anything up to protect the score.
- Whatever you miss becomes the next steps rather than a mark against you.
- `10 questions, mcq/ncert_exemplar/pyq, medium, mixed, unseen only`
- *done when: checkpoint — 8 answered at 70%*

---

## Gravitation — advanced, exam_soon

`phy_11_ch8` · student: no record · scope holds 144 questions, 2 video(s), 1 note set(s) · 1 groundwork candidate(s)

Generated in 18785 ms — 4076 in / 1326 out / 1664 cached.

### Generated

> Finish Acceleration due to gravity (variation with height/depth/latitude) fast: one focused learn pass, then easy→medium practice, then hard-level checkpoint (all unseen).

**1. [learn] One-pass concept setup (Variation of g)**  
*You have no attempt history here and your exam is soon, so start with a quick, focused pass before jumping into questions.*

- Watch the video end-to-end at 1.25×–1.5× speed without pausing; only pause to mark timestamps where you feel unsure.
- Immediately after the video, open the chapter notes and skim only the parts that match the same subtopic; highlight/mark 5–8 lines you want to revisit later.
- Write a 6–8 point checklist of sub-cases you must be able to identify (no solutions, just headings).
- Stop this step as soon as your checklist is ready—don’t start questions yet.
- `video: aaaaaaaaaaa`
- `notes: phy_11_ch8`
- *done when: self — student marks it*

**2. [practise] Easy accuracy build (MCQs)**  
*Lock in the basics quickly so medium/hard questions don’t become guesswork.*

- Do 12 questions total: 4 each from height, depth, and latitude (easy MCQ), mixed order.
- Solve in sets of 4; after each set, check solutions and write 1-line reason for every mistake in your notebook.
- If you get 2 wrong in any concept, immediately do 2 more easy MCQs from that same concept before moving on.
- Keep time tight; don’t spend more than ~2 minutes per question on this step.
- `12 questions, mcq, easy, mixed`
- *done when: accuracy — 12 answered at 75%*

**3. [practise] Medium application (NCERT Exemplar)**  
*Medium exemplars will force you to apply the idea across different framings before you attempt hard-level verification.*

- Do 9 questions total: 3 each from height, depth, and latitude (medium NCERT Exemplar), mixed.
- Attempt all 9 first without checking; then review all at once and log mistakes by concept (H/D/L).
- Re-open the chapter notes for exactly the concept(s) where you missed questions, and reattempt only those missed questions once.
- If you finish early, use remaining time to redo any 2 questions you found slowest (not the easiest ones).
- `9 questions, ncert_exemplar, medium, mixed`
- `notes: phy_11_ch8`
- *done when: accuracy — 9 answered at 67%*

**4. [practise] Hard practice warm-up (PYQs)**  
*A short hard set will prime you for the final checkpoint at your target difficulty.*

- Do 6 questions total: 2 each from height, depth, and latitude (hard PYQ), mixed.
- After each question, decide if the miss was concept-mixup, careless, or time-pressure; write just the tag next to it.
- Reattempt only the ones you got wrong once, immediately, without looking at notes.
- Stop when you’ve reattempted wrong ones—save energy for the checkpoint.
- `6 questions, pyq, hard, mixed`
- *done when: accuracy — 6 answered at 50%*

**5. [verify] Checkpoint: hard mixed (unseen)**  
*This confirms you can handle exam-level questions on this whole area under pressure.*

- Do the questions in one sitting, timed; no notes/video while attempting.
- After submitting, review every incorrect question and note the concept tag (height/depth/latitude).
- If accuracy is below your comfort level, redo only the incorrect ones the next day before moving on to the next chapter.
- `12 questions, mcq/ncert_exemplar/pyq, hard, mixed, unseen only`
- *done when: checkpoint — 12 answered at 60%*

### Deterministic, for comparison

> 5 steps through Gravitation, about 1h 26m of work.

**1. [learn] Get the picture first**  
*A pass over the whole idea before you start answering, so the questions are recognition rather than guesswork.*

- Do not go through this end to end. Use it only for the concepts named on this step.
- Give it fifteen minutes at most — this close to an exam, questions teach faster than explanations.
- `video: bbbbbbbbbbb`
- *done when: self — student marks it*

**2. [practise] Find your feet**  
*A gentle set across the whole of Gravitation, to show you where you already stand.*

- Answer every one, including the ones you would normally skip. A guess still shows you where the gap is.
- Check the whole set in one go and write down only the concepts you missed.
- `8 questions, mcq/ncert_exemplar/pyq, easy/unrated, easiest_first`
- *done when: accuracy — 6 answered at 60%*

**3. [practise] See how it is actually asked**  
*Past questions combine ideas rather than testing one, which is the part practice on its own never prepares you for.*

- Sit these like the real thing: one sitting, no notes, no checking as you go.
- Afterwards, sort your mistakes into 'did not know' and 'knew but slipped'. They need different fixes.
- `10 questions, pyq, hard, mixed`
- *done when: accuracy — 7 answered at 60%*

**4. [consolidate] Tidy it up before the check**  
*A short pass over what you have been getting wrong, before it is measured.*

- One pass, fifteen minutes, only the concepts you missed above.
- Write them on a single sheet you can look at the morning of the exam.
- `notes: phy_11_ch8`
- *done when: self — student marks it*

**5. [verify] Check you have Gravitation**  
*Questions you have not seen, at the level you are aiming for. This is what finishing the plan means.*

- Treat this as the exam: one sitting, timed by you, nothing open beside it.
- Answer everything, including guesses, exactly as you would on the day.
- What you miss here is what to spend your remaining time on.
- `10 questions, mcq/ncert_exemplar/pyq, hard, mixed, unseen only`
- *done when: checkpoint — 8 answered at 70%*

---

## Gravitational field — basic, first_time

`phy_11_ch8_t1` · student: has a record · scope holds 144 questions, 1 video(s), 1 note set(s) · 1 groundwork candidate(s)

Generated in 16446 ms — 4061 in / 1290 out / 1664 cached.

### Generated

> You’ve started Gravitational field but coverage is low, and your biggest gap is Variation of g with depth (many wrong, including hard/medium). We’ll quickly patch the foundation skill (Newton’s second law), then rebuild the topic with the video + chapter notes, followed by easy-first practice, and finish with an easy checkpoint of unseen questions across height/depth/latitude.

**1. [practise] · **groundwork** Foundation fix: Newton’s second law (targeted warm-up)**  
*Your accuracy in the prerequisite Newton’s second law is low, and it’s likely causing avoidable errors when you try g-variation questions.*

- Open the questions and do 8 in one sitting, mixed order, without hints.
- After each wrong question, write 1-line reason (e.g., setup mistake / direction / units) in your error log.
- Re-attempt only the wrong ones immediately once (same step) before moving on.
- `8 questions, mcq, easy, mixed`
- *done when: accuracy — 8 answered at 60%*

**2. [learn] One-pass concept build: Acceleration due to gravity (video)**  
*Before more practice, get one clean pass through the exact subtopic you’re working in so your approach stays consistent.*

- Watch the full video once without pausing.
- On a second quick skim, pause only to jot a 6–8 bullet ‘mini-sheet’ of the key cases you must remember for height/depth/latitude.
- Mark any point you felt unsure about with a star to revisit during practice review.
- `video: aaaaaaaaaaa`
- *done when: self — student marks it*

**3. [learn] Tighten from notes: Gravitation — chapter notes (only g-variation parts)**  
*Your depth accuracy is currently weak, so you need a quick written reference to lock in the cases and prevent repeat mistakes.*

- Open the chapter notes and read only the parts covering Acceleration due to gravity + variation with height/depth/latitude.
- Add 5 more bullets to the same mini-sheet you started in the video step (keep it to one page).
- At the end, close notes and try to recall your mini-sheet once; reopen only to fill gaps.
- `notes: phy_11_ch8`
- *done when: self — student marks it*

**4. [practise] Easy MCQs to stabilize: depth + height + latitude**  
*You need quick, confidence-building reps at easy level—especially for depth—before touching medium/hard again.*

- Do these 12 questions in 3 batches of 4; check answers only after each batch.
- For every wrong one, tag it as depth/height/latitude in your error log and write a 1-line fix.
- After finishing all 12, redo the wrong ones once immediately (still without notes).
- `5 questions, mcq, easy, easiest_first`
- `4 questions, mcq, easy, easiest_first`
- `3 questions, mcq, easy, easiest_first`
- *done when: accuracy — 12 answered at 70%*

**5. [verify] Checkpoint (easy, unseen): Gravitational field — full scope**  
*This confirms you can handle easy NEET-level questions across height, depth, and latitude without repeats.*

- Attempt all questions in one go, timed (aim ~1–1.5 min each).
- Do not open notes/video during the checkpoint.
- After submission, list the concepts of every wrong question (depth/height/latitude) to decide what to repeat next.
- `15 questions, mcq/ncert_exemplar/pyq, easy, mixed, unseen only`
- *done when: checkpoint — 15 answered at 70%*

### Deterministic, for comparison

> 6 steps through Gravitational field, about 1h 42m of work.

**1. [practise] · **groundwork** Shore up Newton's second law**  
*Gravitational field leans on this from Laws of Motion, and your answers there are coming out wrong more often than not.*

- This is groundwork from an earlier chapter, not the topic you asked about.
- Do these before anything else — the rest of the plan assumes them.
- If you get most of them right quickly, mark the step done and move on.
- `5 questions, mcq/ncert_exemplar/pyq, easy/medium, easiest_first`
- *done when: accuracy — 5 answered at 60%*

**2. [learn] Get the picture first**  
*A pass over the whole idea before you start answering, so the questions are recognition rather than guesswork.*

- Go through it once without taking notes — you are building a picture, not a record.
- Then go back and write down only the parts that surprised you.
- If a derivation loses you, mark where and keep going; the practice below will tell you whether it mattered.
- `video: aaaaaaaaaaa`
- *done when: self — student marks it*

**3. [practise] Find your feet**  
*A gentle set across the whole of Gravitational field, to show you where you already stand.*

- Do the first three without looking anything up, even if you are stuck.
- Check all three at once rather than one at a time — a pattern of mistakes tells you more than a single one.
- For anything you got wrong, open Understand with AI before starting the next batch.
- `8 questions, mcq/ncert_exemplar/pyq, easy/unrated, easiest_first`
- *done when: accuracy — 6 answered at 60%*

**4. [practise] Go at what is not sticking**  
*Variation of g with depth accounts for more of your wrong answers here than anything else.*

- These are the concepts your answers say are shakiest, not the ones that look hardest.
- Go slowly. Speed here just repeats the same mistake faster.
- Read the solution for every one you get wrong, including the ones you nearly had.
- `8 questions, mcq/ncert_exemplar/pyq, easy, mixed`
- *done when: accuracy — 6 answered at 60%*

**5. [practise] See how it is actually asked**  
*Past questions combine ideas rather than testing one, which is the part practice on its own never prepares you for.*

- These are real exam questions, so expect them to combine ideas rather than test one.
- Before answering, name which concept the question is really about; that habit is worth more than the answer.
- `10 questions, pyq, easy, mixed`
- *done when: accuracy — 7 answered at 60%*

**6. [verify] Check you have Gravitational field**  
*Questions you have not seen, at the level you are aiming for. This is what finishing the plan means.*

- Sit this in one go, without notes, and without checking anything as you answer.
- Nothing is revealed until you have answered every question — that is deliberate, and it is the only way this tells you anything true.
- If you miss a few, the plan adds work on exactly those concepts. You do not start over.
- `10 questions, mcq/ncert_exemplar/pyq, easy, mixed, unseen only`
- *done when: checkpoint — 8 answered at 70%*

---

## The second law — intermediate, revising

`phy_11_ch5_s1` · student: has a record · scope holds 48 questions, 0 video(s), 0 note set(s) · 0 groundwork candidate(s)

Generated in 13128 ms — 2908 in / 957 out / 1664 cached.

### Generated

> You’ve attempted only a small part of this subtopic and accuracy is low, so we’ll rebuild confidence with easy MCQs, then move to medium (MCQ + Exemplar), and finish with a medium checkpoint using unseen questions.

**1. [practise] Reset with easy MCQs (accuracy first)**  
*Your easy-level accuracy is 2/4, so tightening basics here will stop the same mistakes from repeating at medium level.*

- Do 4 easy MCQs on Newton’s second law in one go, without switching tabs or revisiting attempts mid-way.
- After submitting, write a 1-line note for each wrong question: what you did vs what the question wanted (keep it short).
- Immediately redo only the wrong ones once (from your attempt review screen) before moving on.
- `4 questions, mcq, easy, easiest_first`
- *done when: accuracy — 4 answered at 75%*

**2. [practise] Bridge to medium with MCQs (first medium exposure)**  
*You haven’t attempted any medium questions yet, so this step moves you up a level while keeping the format familiar.*

- Attempt 4 medium MCQs mixed order, but stop after every 2 questions to quickly check only what you marked (no deep review yet).
- After all 4, review solutions and tag each mistake as either: setup/interpretation/careless.
- Redo the ones you got wrong once, immediately, aiming for a clean second attempt.
- `4 questions, mcq, medium, mixed`
- *done when: accuracy — 4 answered at 60%*

**3. [practise] Medium reinforcement with NCERT Exemplar**  
*Switching to Exemplar at the same difficulty helps you confirm you can apply the idea beyond your MCQ pattern.*

- Do 4 medium NCERT Exemplar questions in one sitting.
- For each wrong question, note the exact point where you got stuck (start/after reading/last step).
- End the step by re-attempting only the wrong ones once, without looking at your previous selection.
- `4 questions, ncert_exemplar, medium, mixed`
- *done when: accuracy — 4 answered at 60%*

**4. [verify] Checkpoint (Medium, unseen mix)**  
*This verifies you can score on Newton’s second law at NEET-relevant medium level with fresh questions.*

- Attempt all questions in one go under light timer pressure (don’t pause between them).
- Don’t review after each question; review only after submitting the full set.
- After the score, open only the wrong ones and write a 3-bullet fix list you’ll follow next time (what to check, what to avoid, what to do first).
- `8 questions, mcq/ncert_exemplar/pyq, medium, mixed, unseen only`
- *done when: checkpoint — 8 answered at 60%*

### Deterministic, for comparison

> 4 steps through The second law, about 1h 12m of work.

**1. [practise] Find your feet**  
*A gentle set across the whole of The second law, to show you where you already stand.*

- Work in batches of four and check each batch together.
- If a question takes more than two minutes, that is not revision, it is relearning — mark it and come back.
- `8 questions, mcq/ncert_exemplar/pyq, easy/unrated, easiest_first`
- *done when: accuracy — 6 answered at 60%*

**2. [practise] Go at what is not sticking**  
*Newton's second law accounts for more of your wrong answers here than anything else.*

- Straight to the concepts you have been missing — there is no warm-up in this step.
- After each wrong answer, say out loud what you would do differently before moving on.
- `8 questions, mcq/ncert_exemplar/pyq, medium, mixed`
- *done when: accuracy — 6 answered at 60%*

**3. [practise] See how it is actually asked**  
*Past questions combine ideas rather than testing one, which is the part practice on its own never prepares you for.*

- Give yourself roughly a minute a question and stick to it.
- Check at the end, not as you go — stopping to check mid-set hides how you would actually perform.
- `10 questions, pyq, medium, mixed`
- *done when: accuracy — 7 answered at 60%*

**4. [verify] Check you have The second law**  
*Questions you have not seen, at the level you are aiming for. This is what finishing the plan means.*

- One sitting, no notes, no checking as you go.
- This is the measurement the whole plan has been building to, so do not look anything up to protect the score.
- Whatever you miss becomes the next steps rather than a mark against you.
- `10 questions, mcq/ncert_exemplar/pyq, medium, mixed, unseen only`
- *done when: checkpoint — 8 answered at 70%*

---

## The second law — basic, first_time

`phy_11_ch5_s1` · student: no record · scope holds 48 questions, 0 video(s), 0 note set(s) · 0 groundwork candidate(s)

Generated in 15852 ms — 2873 in / 1206 out / 1664 cached.

### Generated

> You don’t have videos/notes/tests in this scope, so we’ll build your first-time prep entirely through a short ladder of easy questions across MCQ → NCERT Exemplar → PYQ, ending with an easy checkpoint on unseen questions.

**1. [practise] Warm-up: Easy MCQs (first contact)**  
*You’re starting this subtopic for the first time, so begin with the most straightforward easy set to get familiar and reduce silly errors early.*

- Do 4 questions in one go (no pausing in between).
- After each question, quickly tag it in your head as: (A) confident, (B) unsure, (C) guessed.
- Write a 1-line note for every wrong/unsure question (what you’ll avoid next time).
- Stop as soon as you finish all 4; don’t add extra questions yet.
- `4 questions, mcq, easy, easiest_first`
- *done when: accuracy — 4 answered at 75%*

**2. [practise] Reinforce: Easy NCERT Exemplar**  
*Now that you’ve seen the basic patterns once, use Exemplar-level easy questions to stabilize your approach on standard cases.*

- Attempt 4 questions in 2 blocks of 2; check only after each block.
- For every wrong one, immediately re-attempt it once before moving on.
- Add 2 bullet points to your notes: the mistake type + the fix (no long solutions).
- If you hit a time sink, mark it and move forward; don’t get stuck.
- `4 questions, ncert_exemplar, easy, mixed`
- *done when: accuracy — 4 answered at 75%*

**3. [practise] Easy PYQs: Exam-style check**  
*PYQs will show you how this subtopic is typically tested in NEET-style wording, while staying at easy difficulty.*

- Attempt 4 questions timed (set a strict per-question time limit).
- After finishing all 4, review wrong/unsure ones and re-attempt only those once.
- Write a mini-checklist (3 items max) of what you must verify before locking an option.
- Don’t redo correct+confident questions.
- `4 questions, pyq, easy, mixed`
- *done when: accuracy — 4 answered at 75%*

**4. [consolidate] Mixed easy drill (MCQ + Exemplar)**  
*A short mixed set will force quick switching and lock in consistency before the final checkpoint.*

- Do 6 questions total: 3 MCQ (easy) + 3 Exemplar (easy), mixed order.
- Check answers only after all 6 are attempted.
- For each wrong question, write exactly one sentence: what fooled you.
- End by rereading your one-line notes from Steps 1–3 (no reattempts here).
- `6 questions, mcq/ncert_exemplar, easy, mixed`
- *done when: accuracy — 6 answered at 75%*

**5. [verify] Checkpoint: Easy unseen questions (full scope)**  
*This confirms you can score reliably on easy questions in this subtopic without repeating the same items.*

- Attempt the full set in one sitting, timed.
- Do not revisit any question until you’ve attempted all of them.
- After submission, list only the concepts/mistake-types behind wrong answers (no long work).
- If your accuracy is below target, redo Steps 3–4 next, not Step 1.
- `10 questions, mcq/ncert_exemplar/pyq, easy, mixed, unseen only`
- *done when: checkpoint — 10 answered at 80%*

### Deterministic, for comparison

> 3 steps through The second law, about 56 minutes of work.

**1. [practise] Find your feet**  
*A gentle set across the whole of The second law, to show you where you already stand.*

- Do the first three without looking anything up, even if you are stuck.
- Check all three at once rather than one at a time — a pattern of mistakes tells you more than a single one.
- For anything you got wrong, open Understand with AI before starting the next batch.
- `8 questions, mcq/ncert_exemplar/pyq, easy/unrated, easiest_first`
- *done when: accuracy — 6 answered at 60%*

**2. [practise] See how it is actually asked**  
*Past questions combine ideas rather than testing one, which is the part practice on its own never prepares you for.*

- These are real exam questions, so expect them to combine ideas rather than test one.
- Before answering, name which concept the question is really about; that habit is worth more than the answer.
- `10 questions, pyq, easy, mixed`
- *done when: accuracy — 7 answered at 60%*

**3. [verify] Check you have The second law**  
*Questions you have not seen, at the level you are aiming for. This is what finishing the plan means.*

- Sit this in one go, without notes, and without checking anything as you answer.
- Nothing is revealed until you have answered every question — that is deliberate, and it is the only way this tells you anything true.
- If you miss a few, the plan adds work on exactly those concepts. You do not start over.
- `10 questions, mcq/ncert_exemplar/pyq, easy, mixed, unseen only`
- *done when: checkpoint — 8 answered at 70%*

---

## Laws of Motion — intermediate, exam_soon

`phy_11_ch5` · student: no record · scope holds 48 questions, 0 video(s), 0 note set(s) · 0 groundwork candidate(s)

Generated in 15242 ms — 2873 in / 1143 out / 1664 cached.

### Generated

> You have no attempt history here and there are no videos/notes/tests, so we’ll build momentum using questions only: start easy (MCQ), then add NCERT Exemplar and PYQs, and finish with a medium-difficulty checkpoint on unseen questions.

**1. [practise] Warm-up: Easy MCQs (Newton’s second law)**  
*This gives you a quick entry into the chapter and sets a baseline without overwhelming difficulty.*

- Do 4 questions in one sitting, then pause and mark each as Sure/Unsure in your rough notebook.
- For every wrong/unsure one, write a 1-line tag about what went wrong (setup/units/reading/logic).
- Redo only the wrong/unsure ones immediately (without changing any earlier working), then move on.
- `4 questions, mcq, easy, easiest_first`
- *done when: accuracy — 4 answered at 75%*

**2. [practise] Build core variety: Easy NCERT Exemplar**  
*NCERT Exemplar will broaden the way questions are asked while staying at a manageable level.*

- Attempt all 4 questions timed (aim for steady pace, no pausing between them).
- After attempting, list the question numbers you got wrong/unsure and the same 1-line error tag as Step 1.
- Re-attempt those wrong/unsure questions once more immediately.
- `4 questions, ncert_exemplar, easy, mixed`
- *done when: accuracy — 4 answered at 75%*

**3. [practise] Exam-style start: Easy PYQs**  
*PYQs will align your practice to how NEET tends to test this concept.*

- Do all 4 questions in one go (no hints, no revisiting until the end).
- For each wrong/unsure, write what you’ll do differently next time (e.g., ‘draw first’, ‘check sign’, ‘identify system’).
- If you miss 2 or more, redo the entire set once after a 5-minute break.
- `4 questions, pyq, easy, mixed`
- *done when: accuracy — 4 answered at 75%*

**4. [practise] Step up: Medium MCQs (unseen)**  
*This is the bridge from warm-up to your target level, using fresh questions.*

- Attempt 4 questions timed; don’t spend too long on any single one—mark and move.
- After finishing, re-attempt only the ones you marked/ got wrong (max 10 minutes).
- Add 3 bullet ‘rules to remember’ in your notebook based only on what you missed today.
- `4 questions, mcq, medium, mixed, unseen only`
- *done when: accuracy — 4 answered at 60%*

**5. [verify] Checkpoint: Medium mixed questions (unseen)**  
*This measures whether you can now handle Newton’s second law at the level you’re aiming for under exam-style conditions.*

- Do 8 questions in one sitting from a mixed set; treat it like a mini-test.
- Do not redo during the attempt; only review after submitting all 8.
- Note your accuracy + the top 2 recurring error tags to decide what to practise next.
- `8 questions, mcq/ncert_exemplar/pyq, medium, mixed, unseen only`
- *done when: checkpoint — 8 answered at 65%*

### Deterministic, for comparison

> 3 steps through Laws of Motion, about 56 minutes of work.

**1. [practise] Find your feet**  
*A gentle set across the whole of Laws of Motion, to show you where you already stand.*

- Answer every one, including the ones you would normally skip. A guess still shows you where the gap is.
- Check the whole set in one go and write down only the concepts you missed.
- `8 questions, mcq/ncert_exemplar/pyq, easy/unrated, easiest_first`
- *done when: accuracy — 6 answered at 60%*

**2. [practise] See how it is actually asked**  
*Past questions combine ideas rather than testing one, which is the part practice on its own never prepares you for.*

- Sit these like the real thing: one sitting, no notes, no checking as you go.
- Afterwards, sort your mistakes into 'did not know' and 'knew but slipped'. They need different fixes.
- `10 questions, pyq, medium, mixed`
- *done when: accuracy — 7 answered at 60%*

**3. [verify] Check you have Laws of Motion**  
*Questions you have not seen, at the level you are aiming for. This is what finishing the plan means.*

- Treat this as the exam: one sitting, timed by you, nothing open beside it.
- Answer everything, including guesses, exactly as you would on the day.
- What you miss here is what to spend your remaining time on.
- `10 questions, mcq/ncert_exemplar/pyq, medium, mixed, unseen only`
- *done when: checkpoint — 8 answered at 70%*

---
