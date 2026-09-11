"""Reading and writing doubt threads.

One thread per student per chapter, enforced by a unique index rather than remembered by
this code: a student who opens Gravitation twice continues one conversation instead of
starting a second, and that has to hold when two taps race.

Every answer is stored with what it was built from — the concept ids, the question ids,
whether the notes were used, and what the call cost. That record is the difference between
a feature you can investigate and one you can only apologise for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import asyncpg

from app.doubts.answer import Outcome

logger = logging.getLogger(__name__)

#: Turns handed back to the prompt. Deliberately more than `prompt.MAX_HISTORY_TURNS`
#: trims to, so the trimming decision lives in one place — here we just fetch enough.
HISTORY_DEPTH = 12

#: Messages handed back for one chapter. A student at ten a day who keeps returning to
#: one chapter accumulates without limit, and an answer is around 1,500 characters — so an
#: unbounded read is a few hundred kilobytes of JSON on mobile data every time the sheet is
#: opened. Twenty-five exchanges is far more of one chapter's conversation than anybody
#: scrolls back through.
MAX_THREAD_MESSAGES = 50

#: What a student may send in one doubt. Long enough to paste a question they typed out,
#: short enough that nobody is mailing an essay through a model call. Refused at the
#: boundary rather than truncated: a silently cut-off question gets a confident answer to
#: half of it, which is worse than being asked to shorten it.
MAX_QUESTION_CHARS = 1_000


@dataclass(frozen=True)
class StoredMessage:
    """One turn, as the app shows it."""

    message_id: UUID
    role: str
    text: str
    created_at: datetime
    answered: bool | None
    reported: bool


async def thread_for(
    connection: asyncpg.Connection, uid: str, tenant: str, chapter_id: str
) -> UUID:
    """This student's thread for this chapter, created if this is their first doubt.

    One statement, because two taps can arrive together and a read-then-insert would make
    a second thread for the same chapter — which the unique index would then refuse, at
    the cost of a 500 on a student's first ever question. `ON CONFLICT ... DO UPDATE` gets
    the id back either way, and touching `last_message_at` is wanted regardless.
    """
    return await connection.fetchval(
        """
        INSERT INTO doubt_threads (firebase_uid, tenant_id, chapter_id)
             VALUES ($1, $2, $3)
        ON CONFLICT (firebase_uid, chapter_id)
          DO UPDATE SET last_message_at = now()
          RETURNING thread_id
        """,
        uid, tenant, chapter_id,
    )


async def history(
    connection: asyncpg.Connection, thread_id: UUID, depth: int = HISTORY_DEPTH
) -> list[tuple[str, str]]:
    """The last few turns, oldest first, as `(role, text)` for the prompt.

    Fetched newest-first and reversed, because "the last twelve" needs an index on the
    recent end and "oldest first" is what a conversation reads like.
    """
    rows = await connection.fetch(
        """
        SELECT role, text FROM doubt_messages
         WHERE thread_id = $1
         ORDER BY created_at DESC, message_id
         LIMIT $2
        """,
        thread_id, depth,
    )
    return [(r["role"], r["text"]) for r in reversed(rows)]


async def conversation(
    connection: asyncpg.Connection, thread_id: UUID
) -> list[StoredMessage]:
    """The thread's most recent messages, oldest first, for the screen.

    Bounded — see `MAX_THREAD_MESSAGES`. Taken from the recent end and then reversed,
    because the cap has to drop the oldest: a conversation truncated at the *new* end
    would open on something the student said months ago with their last answer missing.
    """
    rows = await connection.fetch(
        """
        SELECT message_id, role, text, created_at, answered, reported
          FROM (
            SELECT message_id, role, text, created_at, answered, reported
              FROM doubt_messages
             WHERE thread_id = $1
             ORDER BY created_at DESC, message_id DESC
             LIMIT $2
          ) recent
         ORDER BY created_at, message_id
        """,
        thread_id, MAX_THREAD_MESSAGES,
    )
    return [StoredMessage(**dict(r)) for r in rows]


async def record(
    connection: asyncpg.Connection,
    *,
    thread_id: UUID,
    uid: str,
    question: str,
    anchor_kind: str,
    anchor_id: str,
    outcome: Outcome,
    model: str | None,
) -> StoredMessage:
    """Store the question and the answer together, or store neither.

    In one transaction because they are one exchange. A question saved without its answer
    reads, on the student's screen, as Jeene ignoring them — and it would still count
    against their ten.
    """
    usage = outcome.usage
    async with connection.transaction():
        await connection.execute(
            """
            INSERT INTO doubt_messages (thread_id, firebase_uid, role, text,
                                        anchor_kind, anchor_id)
                 VALUES ($1, $2, 'student', $3, $4, $5)
            """,
            thread_id, uid, question, anchor_kind, anchor_id,
        )
        row = await connection.fetchrow(
            """
            INSERT INTO doubt_messages (thread_id, firebase_uid, role, text,
                                        used_concept_ids, used_question_ids, used_notes,
                                        answered, model, tokens_in, tokens_out)
                 VALUES ($1, $2, 'jeene', $3, $4::text[], $5::text[], $6, $7, $8, $9, $10)
              RETURNING message_id, role, text, created_at, answered, reported
            """,
            thread_id, uid, outcome.text,
            outcome.used_concept_ids, outcome.used_question_ids, outcome.used_notes,
            outcome.answered, model,
            usage.input_tokens if usage else None,
            usage.output_tokens if usage else None,
        )
        await connection.execute(
            "UPDATE doubt_threads SET last_message_at = now() WHERE thread_id = $1",
            thread_id,
        )
    return StoredMessage(**dict(row))


async def mark_reported(
    connection: asyncpg.Connection, message_id: UUID, uid: str
) -> bool:
    """A student saying an answer was wrong. False if it is not theirs to report.

    Scoped to their own messages, and only to Jeene's half of them — reporting somebody
    else's conversation, or your own question, is not a thing this means.
    """
    updated = await connection.fetchval(
        """
        UPDATE doubt_messages SET reported = TRUE
         WHERE message_id = $1 AND firebase_uid = $2 AND role = 'jeene'
         RETURNING message_id
        """,
        message_id, uid,
    )
    if updated is not None:
        logger.info("doubt answer reported message=%s", message_id)
    return updated is not None
