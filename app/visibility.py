"""Which questions a student is allowed to meet at all.

Separate from `figures.py`, which decides which *parts* of a question may be shown.
This decides whether the question exists for this reader in the first place.

One rule so far, and it is a correctness rule: a question that arrived with a test
paper must not appear in ordinary browsing until that paper is released, or a student
can sit the test in the practice deck the week before. Extracted here when the study
planner became the second reader that has to honour it — a second copy of five lines
of SQL is how the figure-placement rule got out of step with itself, and that cost a
leaked answer.
"""

from __future__ import annotations

# Appended to a WHERE clause over `questions q`.
#
# A generated paper draws on questions that already live in the bank, and putting one
# of those into an unreleased test must not pull it out of practice — only questions
# that ARRIVED with a paper are gated.
NOT_UNRELEASED_TEST_SQL = """
  AND (q.source <> 'test_paper' OR EXISTS (
        SELECT 1 FROM test_questions tq
        JOIN tests t ON t.test_id = tq.test_id
        WHERE tq.question_id = q.question_id AND t.released_at IS NOT NULL))
"""
