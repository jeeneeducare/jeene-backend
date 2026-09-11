"""Ask Jeene: doubts asked while studying a chapter.

Three routes, and the order of what happens inside the first one is most of the design.

A doubt arrives with an anchor — the question card, the notes reader, the topic sheet or
the chapter screen it was typed from. The anchor is resolved against the database before
anything else, because it decides which chapter's material the answer may be built from,
and an anchor the student could not have been looking at is not a question worth spending
a model call on.

Then the gate, then the material, then the model, then the record. The gate comes before
the model and the record comes after it, which is the only ordering where a student is
never charged for an answer they did not receive.

Distinct from Jeene Mode, which plans a chapter. This one only ever answers a doubt about
the chapter in front of them, and the two share nothing but a provider port.
"""

from __future__ import annotations

import logging
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from app.auth import current_tenant, require_user
from app.billing import gates
from app.db import get_connection
from app.doubts import answer as answer_module
from app.doubts import context, store
from app.doubts.provider import build_doubt_provider
from app.providers.base import ProviderError
from app.schemas import DoubtAsk, DoubtMessage, DoubtReply, DoubtThread

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/doubts", tags=["doubts"])

#: Said when the feature is not configured on this deployment. A 503 rather than a 404,
#: because the route exists and the app should show "not available just now" rather than
#: conclude the endpoint is gone.
UNAVAILABLE = "Ask Jeene is not available right now."

#: Said when the anchor does not resolve. Deliberately vague about why: the student cannot
#: act on "that node is unpublished", and the three reasons are one thing to them.
NO_ANCHOR = "I could not find what you are asking about."


def _as_message(message: store.StoredMessage) -> DoubtMessage:
    return DoubtMessage(
        message_id=message.message_id,
        role=message.role,
        text=message.text,
        created_at=message.created_at,
        answered=message.answered,
        reported=message.reported,
    )


@router.post("/ask", response_model=DoubtReply)
async def ask(
    body: DoubtAsk,
    tenant: str = Depends(current_tenant),
    user: dict = Depends(require_user),
    connection: asyncpg.Connection = Depends(get_connection),
) -> DoubtReply:
    """Answer one doubt, from the material of the chapter it was asked in.

    The refusals a student can meet here, and why each is the answer it is:

    * **503** — not configured. Nothing is wrong with their question.
    * **404** — the anchor does not resolve, so there is no chapter to answer from.
    * **402** — not Pro, or out of doubts for today. The body says which.
    * **422** — nothing in this chapter to answer from. A model call against an empty
      material block would produce exactly the confident invention this feature exists to
      prevent, so it is refused before it is made rather than after.
    * **503** — the provider failed. Distinct from a refusal: the question was fine and
      trying again in a minute is the right advice.
    """
    provider = build_doubt_provider()
    if provider is None:
        raise HTTPException(status_code=503, detail=UNAVAILABLE)

    anchor = await context.resolve(connection, body.anchor_kind, body.anchor_id, tenant)
    if anchor is None:
        raise HTTPException(status_code=404, detail=NO_ANCHOR)

    # Before the model, never after. A student refused here has not spent a doubt.
    await gates.ensure_can_ask(connection, user["uid"], tenant)

    material = await context.gather(
        connection, anchor, body.text, user["uid"], tenant
    )
    if material.is_empty():
        logger.warning("no material for a doubt chapter=%s", anchor.chapter_id)
        raise HTTPException(
            status_code=422,
            detail="There is nothing in this chapter for me to answer from yet.",
        )

    thread_id = await store.thread_for(
        connection, user["uid"], tenant, anchor.chapter_id
    )
    history = await store.history(connection, thread_id)

    try:
        outcome = await answer_module.answer(provider, material, body.text, history)
    except ProviderError:
        # Logged without the question: a student's words are their conversation, and this
        # line goes to a third-party log aggregator.
        logger.warning("the provider could not answer a doubt chapter=%s",
                       anchor.chapter_id)
        raise HTTPException(
            status_code=503,
            detail="I could not reach my notes just now. Try again in a moment.",
        ) from None

    message = await store.record(
        connection,
        thread_id=thread_id,
        uid=user["uid"],
        question=body.text,
        anchor_kind=body.anchor_kind,
        anchor_id=body.anchor_id,
        outcome=outcome,
        model=outcome.usage.model if outcome.usage else None,
    )

    return DoubtReply(
        thread_id=thread_id,
        message=_as_message(message),
        doubts_left_today=await gates.doubts_left_today(connection, user["uid"]),
        chapter_id=anchor.chapter_id,
        chapter_title=anchor.chapter_title,
    )


@router.get("/chapters/{chapter_id}", response_model=DoubtThread)
async def thread(
    chapter_id: str,
    tenant: str = Depends(current_tenant),
    user: dict = Depends(require_user),
    connection: asyncpg.Connection = Depends(get_connection),
) -> DoubtThread:
    """This student's conversation about this chapter, so reopening it continues it.

    Not gated. Reading back what they have already been told is not a model call and does
    not spend a doubt — and a student whose Pro has lapsed should still be able to read
    what they were told while it was current.
    """
    row = await connection.fetchrow(
        """
        SELECT t.thread_id, t.chapter_id, n.title AS chapter_title
          FROM doubt_threads t
          JOIN nodes n ON n.node_id = t.chapter_id
         WHERE t.firebase_uid = $1 AND t.chapter_id = $2 AND t.tenant_id = $3
        """,
        user["uid"], chapter_id, tenant,
    )
    left = await gates.doubts_left_today(connection, user["uid"])
    # The wall they would meet if they asked now, so the sheet says so before they type.
    gate = await gates.doubt_gate(connection, user["uid"], tenant)
    if row is None:
        # Not a 404. "You have not asked anything about this chapter yet" is an empty
        # thread, and an empty screen is what the app should draw for it.
        title = await connection.fetchval(
            "SELECT title FROM nodes WHERE node_id = $1 AND tenant_id = $2",
            chapter_id, tenant,
        )
        if title is None:
            raise HTTPException(status_code=404, detail="No such chapter")
        return DoubtThread(
            thread_id=UUID(int=0), chapter_id=chapter_id, chapter_title=title,
            messages=[], doubts_left_today=left, gate=gate,
        )

    messages = await store.conversation(connection, row["thread_id"])
    return DoubtThread(
        thread_id=row["thread_id"],
        chapter_id=row["chapter_id"],
        chapter_title=row["chapter_title"],
        messages=[_as_message(m) for m in messages],
        doubts_left_today=left,
        gate=gate,
    )


@router.post("/messages/{message_id}/report", status_code=204)
async def report(
    message_id: UUID,
    user: dict = Depends(require_user),
    connection: asyncpg.Connection = Depends(get_connection),
) -> None:
    """"This was wrong."

    The best bug reports this feature will get, and there is nowhere else they could come
    from: an answer is assembled from one student's material at one moment, and without
    this nobody would ever know which ones were bad.

    204 either way a student can reach — reporting the same answer twice is not an error,
    and telling somebody their complaint was rejected on a technicality is not a thing
    worth building.
    """
    if not await store.mark_reported(connection, message_id, user["uid"]):
        raise HTTPException(status_code=404, detail="No such answer")
