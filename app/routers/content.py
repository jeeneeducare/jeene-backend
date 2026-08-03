import re

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from app.auth import current_tenant, require_user
from app.db import get_connection
from app.schemas import (
    QuestionExplanation,
    Chapter,
    ConceptTag,
    PaginatedQuestions,
    Question,
    QuestionAnswer,
    ChapterNotes,
    ChapterVideo,
    VideoGroup,
    QuestionFigure,
    QuestionHistory,
    TreeNode,
)

router = APIRouter()
# A question that arrived with a test paper stays out of ordinary browsing until a
# test containing it has been released, so a student cannot meet a paper's questions
# in practice before sitting it.
#
# Keyed on the question's own source, NOT merely on membership of an unreleased test.
# A generated paper draws on questions that already live in the bank, and putting one
# of those into an unreleased test must not pull it out of practice — only questions
# that ARRIVED with a paper are gated.
_NOT_UNRELEASED_TEST = """
  AND (q.source <> 'test_paper' OR EXISTS (
        SELECT 1 FROM test_questions tq
        JOIN tests t ON t.test_id = tq.test_id
        WHERE tq.question_id = q.question_id AND t.released_at IS NOT NULL))
"""


@router.get("/chapters", response_model=list[Chapter])
async def list_chapters(
    class_level: int | None = Query(default=None, ge=1),
    exam: str | None = Query(default=None),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[Chapter]:
    rows = await connection.fetch(
        """
        SELECT c.node_id, c.title, c.ncert_chapter_number, c.subject_id, c.class_level,
               s.title AS subject_name
        FROM nodes c
        LEFT JOIN nodes s
            ON s.tenant_id = c.tenant_id AND s.type = 'subject' AND s.subject_id = c.subject_id
        WHERE c.tenant_id = $1 AND c.type = 'chapter' AND c.status = 'published'
          AND ($2::int IS NULL OR c.class_level = $2)
          AND ($3::text IS NULL OR EXISTS (
                SELECT 1 FROM exams e WHERE e.exam_id = $3 AND c.subject_id = ANY(e.subjects)))
        ORDER BY c.subject_id, c.class_level, c.ncert_chapter_number NULLS LAST, c.title
        """,
        tenant,
        class_level,
        exam,
    )
    return [
        Chapter(
            node_id=r["node_id"],
            title=r["title"],
            chapter_number=r["ncert_chapter_number"],
            subject=r["subject_id"],
            subject_name=r["subject_name"],
            class_level=r["class_level"],
        )
        for r in rows
    ]


@router.get("/chapters/{chapter_id}/tree", response_model=TreeNode)
async def get_chapter_tree(
    chapter_id: str,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> TreeNode:
    rows = await _fetch_chapter_subtree(connection, chapter_id, tenant)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Chapter '{chapter_id}' not found")
    return _build_tree(rows, chapter_id)


@router.get("/chapters/{chapter_id}/questions", response_model=PaginatedQuestions)
async def list_chapter_questions(
    chapter_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> PaginatedQuestions:
    rows = await _fetch_chapter_subtree(connection, chapter_id, tenant)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Chapter '{chapter_id}' not found")
    node_ids = [r["node_id"] for r in rows]
    return await _paginated_questions_for_node_ids(connection, node_ids, limit, offset, tenant)


_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")

# The player page, served from our own domain so it has a real origin and sends a real
# referrer. Both are what YouTube's embed checks before it will configure itself, and
# neither can be faked from inside an app: a web view handed HTML with a base URL only
# resolves relative links against it, it does not take on that origin, and a web view
# navigating straight to the embed URL sends no referrer at all. Those two facts cost
# three attempts to learn.
#
# Serving it here rather than bundling it in the apps also means the player can be fixed
# without a release, which after those three attempts is worth something on its own.
_PLAYER_PAGE = """<!doctype html>
<html>
  <head>
    <meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1,
          user-scalable=no">
    <style>
      html, body {{ margin: 0; padding: 0; background: #000; height: 100%; overflow: hidden; }}
      #player {{ width: 100%; height: 100%; }}
    </style>
  </head>
  <body>
    <div id="player"></div>
    <script src="https://www.youtube.com/iframe_api"></script>
    <script>
      function say(what) {{
        // iOS listens on a message handler, Android reads the console. Both are one way.
        if (window.webkit && window.webkit.messageHandlers
            && window.webkit.messageHandlers.player) {{
          window.webkit.messageHandlers.player.postMessage(what);
        }}
        console.log("jeene-player " + what);
      }}
      function onYouTubeIframeAPIReady() {{
        new YT.Player("player", {{
          videoId: "{video_id}",
          playerVars: {{ playsinline: 1, rel: 0, modestbranding: 1, origin: "{origin}" }},
          events: {{
            onReady: function () {{ say("ready"); }},
            onError: function (e) {{ say("error " + e.data); }}
          }}
        }});
      }}
      setTimeout(function () {{
        if (typeof YT === "undefined") say("error -1");
      }}, 8000);
    </script>
  </body>
</html>
"""


@router.get("/player", response_class=HTMLResponse)
async def player(request: Request, v: str) -> HTMLResponse:
    """A page holding one YouTube player, for the apps to load in a web view.

    The video id is checked against the id alphabet before it is put into the page. It
    arrives in a query string, which is to say from anywhere, and it is written into a
    script; without this that is an injection.
    """
    if not _VIDEO_ID.match(v):
        raise HTTPException(status_code=400, detail="Not a YouTube video id")
    origin = str(request.base_url).rstrip("/")
    return HTMLResponse(_PLAYER_PAGE.format(video_id=v, origin=origin))


@router.get("/nodes/{node_id}/videos", response_model=list[ChapterVideo])
async def node_videos(
    node_id: str,
    request: Request,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[ChapterVideo]:
    """The lectures for one place in the tree, or the nearest thing above it.

    A video can be attached to a chapter, a topic, a subtopic or a concept. A student
    opening Video Lectures from a topic should see that topic's videos if there are any,
    and the chapter's if there are not, rather than nothing: a general lecture is still
    better than an empty screen, and the person attaching a chapter-wide video should not
    have to repeat it on every topic underneath.

    So this walks up from the node and returns the first level that has anything. Only
    the first: mixing a topic's own videos with the chapter's would bury the specific one
    under the general.
    """
    rows = await connection.fetch(
        """
        WITH RECURSIVE lineage AS (
            SELECT node_id, parent_id, 0 AS distance
              FROM nodes WHERE node_id = $1 AND tenant_id = $2 AND status = 'published'
            UNION ALL
            SELECT n.node_id, n.parent_id, l.distance + 1
              FROM nodes n JOIN lineage l ON n.node_id = l.parent_id
             WHERE n.tenant_id = $2 AND n.status = 'published'
        ),
        found AS (
            SELECT v.*, l.distance
              FROM node_videos v JOIN lineage l ON l.node_id = v.node_id
             WHERE v.tenant_id = $2 AND v.status = 'published'
        )
        SELECT youtube_id, title, channel, thumbnail_url
          FROM found
         WHERE distance = (SELECT min(distance) FROM found)
         ORDER BY position, added_at
        """,
        node_id, tenant,
    )
    origin = str(request.base_url).rstrip("/")
    return [
        ChapterVideo(**dict(r), player_url=f"{origin}/player?v={r['youtube_id']}")
        for r in rows
    ]


@router.get("/nodes/{node_id}/video-groups", response_model=list[VideoGroup])
async def node_video_groups(
    node_id: str,
    request: Request,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[VideoGroup]:
    """Everything a student standing on this node should be offered, grouped by where it hangs.

    `/nodes/{id}/videos` walks *up* and stops at the first level with anything, which is
    right when the question is "what plays here". It is wrong for a screen, because a
    subtopic with one lecture on each of its concepts looks empty: the walk never looks
    down.

    So this looks down first. The node's own videos, then each descendant that has any, in
    tree order, each labelled with the node it belongs to. Only if there is nothing at or
    below does it fall back to walking up, so a chapter-wide lecture still reaches a
    concept that has none of its own; that group comes back marked inherited so the screen
    can say where it came from rather than implying it lives here.
    """
    below = await connection.fetch(
        """
        WITH RECURSIVE subtree AS (
            SELECT node_id, parent_id, title, type, display_order,
                   ARRAY[coalesce(display_order, 0)] AS ordering, 0 AS depth
              FROM nodes WHERE node_id = $1 AND tenant_id = $2 AND status = 'published'
            UNION ALL
            SELECT n.node_id, n.parent_id, n.title, n.type, n.display_order,
                   s.ordering || coalesce(n.display_order, 0), s.depth + 1
              FROM nodes n JOIN subtree s ON n.parent_id = s.node_id
             WHERE n.tenant_id = $2 AND n.status = 'published'
        )
        SELECT s.node_id, s.title, s.type, s.ordering, s.depth,
               v.youtube_id, v.title AS video_title, v.channel, v.thumbnail_url, v.position
          FROM subtree s
          JOIN node_videos v ON v.node_id = s.node_id
         WHERE v.tenant_id = $2 AND v.status = 'published'
         ORDER BY s.depth, s.ordering, v.position, v.added_at
        """,
        node_id, tenant,
    )

    origin = str(request.base_url).rstrip("/")

    def as_video(row) -> ChapterVideo:
        return ChapterVideo(
            youtube_id=row["youtube_id"], title=row["video_title"],
            channel=row["channel"], thumbnail_url=row["thumbnail_url"],
            player_url=f"{origin}/player?v={row['youtube_id']}",
        )

    if below:
        groups: list[VideoGroup] = []
        for row in below:
            if not groups or groups[-1].node_id != row["node_id"]:
                groups.append(VideoGroup(
                    node_id=row["node_id"], title=row["title"], type=row["type"]))
            groups[-1].videos.append(as_video(row))
        return groups

    # Nothing here or under it. Fall back to whatever a student would inherit.
    above = await node_videos(node_id, request, tenant, connection)
    if not above:
        return []
    owner = await connection.fetchrow(
        """
        WITH RECURSIVE lineage AS (
            SELECT node_id, parent_id, 0 AS distance FROM nodes
             WHERE node_id = $1 AND tenant_id = $2
            UNION ALL
            SELECT n.node_id, n.parent_id, l.distance + 1
              FROM nodes n JOIN lineage l ON n.node_id = l.parent_id WHERE n.tenant_id = $2
        )
        SELECT n.node_id, n.title, n.type FROM lineage l
          JOIN nodes n ON n.node_id = l.node_id
         WHERE EXISTS (SELECT 1 FROM node_videos v
                        WHERE v.node_id = l.node_id AND v.tenant_id = $2
                          AND v.status = 'published')
         ORDER BY l.distance LIMIT 1
        """,
        node_id, tenant,
    )
    if owner is None:
        return []
    return [VideoGroup(node_id=owner["node_id"], title=owner["title"],
                       type=owner["type"], inherited=True, videos=above)]


@router.get("/chapters/{chapter_id}/videos", response_model=list[ChapterVideo])
async def chapter_videos(
    chapter_id: str,
    request: Request,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[ChapterVideo]:
    """The chapter's own lectures. Kept for the app's chapter-level tile."""
    return await node_videos(chapter_id, request, tenant, connection)


@router.get("/chapters/{chapter_id}/notes", response_model=ChapterNotes)
async def chapter_notes(
    chapter_id: str,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> ChapterNotes:
    """The written notes for a chapter, if there are any published.

    404 when a chapter has none, which is most of them today: notes exist for eight
    physics chapters and nowhere else yet. The app asks when a chapter opens so the Notes
    tile can say whether there is anything behind it, rather than letting a student tap
    and find out.
    """
    row = await connection.fetchrow(
        """
        SELECT n.chapter_id, n.title, n.pdf_url, n.page_count, n.size_bytes
          FROM chapter_notes n
          JOIN nodes c ON c.node_id = n.chapter_id
         WHERE n.chapter_id = $1 AND n.tenant_id = $2 AND n.status = 'published'
           AND c.status = 'published'
        """,
        chapter_id, tenant,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No notes for this chapter yet")
    return ChapterNotes(**dict(row))


@router.get("/chapters/{chapter_id}/history", response_model=list[QuestionHistory])
async def chapter_history(
    chapter_id: str,
    user: dict = Depends(require_user),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[QuestionHistory]:
    """What this student has already done with the questions in this chapter.

    The practice deck uses it twice over. Opened normally it redraws each answered
    question exactly as the student left it, right or wrong, rather than presenting work
    they have already done as though it were new. Opened from the Mistake Book it is the
    other way round: the wrong ones are the only ones shown, and they are shown blank.

    Only the most recent attempt per question, because that is the one that describes
    where the student stands. Answering something correctly should replace the record of
    getting it wrong, not sit underneath it.

    Returning the key and the solution here is deliberate and is safe for one specific
    reason: an attempt row only exists once a question has been graded and its answer
    shown. Practice grades on the spot, and a paper writes its attempts at submission,
    never during the sitting. So every question this can speak about is one the student
    has already seen the answer to.
    """
    rows = await _fetch_chapter_subtree(connection, chapter_id, tenant)
    if not rows:
        raise HTTPException(status_code=404, detail=f"Chapter '{chapter_id}' not found")

    history = await connection.fetch(
        """
        SELECT DISTINCT ON (a.question_id)
               a.question_id, a.selected_option_ids, a.is_correct, a.created_at,
               a.session_id IS NOT NULL AS in_a_test,
               q.correct_option_ids, q.explanation_json
          FROM attempts a
          JOIN questions q ON q.question_id = a.question_id
          JOIN question_concept_mappings qcm ON qcm.question_id = q.question_id
         WHERE a.firebase_uid = $1 AND a.tenant_id = $2
           AND qcm.concept_node_id = ANY($3::text[])
           AND q.status = 'published'
        """ + _NOT_UNRELEASED_TEST + """
         ORDER BY a.question_id, a.created_at DESC
        """,
        user["uid"], tenant, [r["node_id"] for r in rows],
    )

    return [
        QuestionHistory(
            question_id=r["question_id"],
            selected_option_ids=list(r["selected_option_ids"] or []),
            is_correct=r["is_correct"],
            correct_option_ids=list(r["correct_option_ids"] or []),
            explanation=(r["explanation_json"] or {}).get("text", ""),
            attempted_at=r["created_at"],
            in_a_test=r["in_a_test"],
        )
        for r in history
    ]


@router.get("/concepts/{concept_id}/questions", response_model=PaginatedQuestions)
async def list_concept_questions(
    concept_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> PaginatedQuestions:
    exists = await connection.fetchval(
        "SELECT 1 FROM nodes WHERE tenant_id = $1 AND node_id = $2 AND type = 'concept' AND status = 'published'",
        tenant,
        concept_id,
    )
    if not exists:
        raise HTTPException(status_code=404, detail=f"Concept '{concept_id}' not found")
    return await _paginated_questions_for_node_ids(connection, [concept_id], limit, offset, tenant)


@router.get("/questions/{question_id}", response_model=Question)
async def get_question(
    question_id: str,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> Question:
    row = await connection.fetchrow(
        """
        SELECT q.question_id, q.question_type, q.question_text, q.options_json, q.difficulty,
               (SELECT array_agg(m.concept_node_id)
                  FROM question_concept_mappings m
                 WHERE m.question_id = q.question_id) AS concept_ids
        FROM questions q
        WHERE q.tenant_id = $1 AND q.question_id = $2 AND q.status = 'published'
        """ + _NOT_UNRELEASED_TEST,
        tenant,
        question_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Question '{question_id}' not found")
    figures = await _fetch_figures(connection, [question_id])
    return _row_to_question(row, figures)


@router.get("/questions/{question_id}/explanation", response_model=QuestionExplanation)
async def get_question_explanation(
    question_id: str,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> QuestionExplanation:
    """A simpler retelling of the worked solution, for a student who is still stuck.

    Served on exactly the same terms as the answer reveal, and gated by the same rule
    for unreleased test papers: an explanation says what the answer is, so a second,
    looser door to the same information would make the first one pointless.

    A plain read. The explanation was written by the pipeline long before anyone asked
    for it, so there is no model call on this path, nothing to rate limit, and nothing
    to keep a student waiting. `draft` explanations are invisible here, exactly like
    every other kind of content.
    """
    row = await connection.fetchrow(
        """
        SELECT e.question_id, e.text
        FROM question_explanations e
        JOIN questions q ON q.question_id = e.question_id
        WHERE q.tenant_id = $1 AND q.question_id = $2
          AND q.status = 'published' AND e.status = 'published'
        """ + _NOT_UNRELEASED_TEST,
        tenant,
        question_id,
    )
    if row is None:
        # Absent rather than empty: most questions will not have one for a while, and
        # the app hides the button rather than offering something that is not there.
        raise HTTPException(
            status_code=404, detail=f"No explanation for '{question_id}' yet"
        )
    return QuestionExplanation(question_id=row["question_id"], text=row["text"])


@router.get("/questions/{question_id}/answer", response_model=QuestionAnswer)
async def get_question_answer(
    question_id: str,
    tenant: str = Depends(current_tenant),
    connection: asyncpg.Connection = Depends(get_connection),
) -> QuestionAnswer:
    row = await connection.fetchrow(
        """
        SELECT q.question_id, q.correct_option_ids, q.explanation_json
        FROM questions q
        WHERE q.tenant_id = $1 AND q.question_id = $2 AND q.status = 'published'
        """ + _NOT_UNRELEASED_TEST,
        tenant,
        question_id,
    )
    if row is None:
        raise HTTPException(status_code=404, detail=f"Question '{question_id}' not found")

    concept_rows = await connection.fetch(
        "SELECT concept_node_id, is_primary FROM question_concept_mappings WHERE question_id = $1",
        question_id,
    )
    return QuestionAnswer(
        question_id=row["question_id"],
        correct_option_ids=list(row["correct_option_ids"] or []),
        explanation=row["explanation_json"],
        concepts=[
            ConceptTag(concept_node_id=r["concept_node_id"], is_primary=r["is_primary"])
            for r in concept_rows
        ],
    )


async def _fetch_chapter_subtree(
    connection: asyncpg.Connection, chapter_id: str, tenant: str
) -> list[asyncpg.Record]:
    return await connection.fetch(
        """
        WITH RECURSIVE descendants AS (
            SELECT node_id, type, title, description, parent_id, depth, display_order
            FROM nodes
            WHERE tenant_id = $1 AND node_id = $2 AND type = 'chapter' AND status = 'published'
            UNION ALL
            SELECT n.node_id, n.type, n.title, n.description, n.parent_id, n.depth, n.display_order
            FROM nodes n
            JOIN descendants d ON n.parent_id = d.node_id
            WHERE n.tenant_id = $1 AND n.status = 'published'
        )
        SELECT * FROM descendants ORDER BY depth, display_order
        """,
        tenant,
        chapter_id,
    )


def _build_tree(rows: list[asyncpg.Record], chapter_id: str) -> TreeNode:
    nodes_by_id = {
        r["node_id"]: TreeNode(
            node_id=r["node_id"],
            type=r["type"],
            title=r["title"],
            description=r["description"],
            children=[],
        )
        for r in rows
    }
    for r in rows:
        if r["node_id"] == chapter_id:
            continue
        parent = nodes_by_id.get(r["parent_id"])
        if parent is not None:
            parent.children.append(nodes_by_id[r["node_id"]])
    return nodes_by_id[chapter_id]


async def _paginated_questions_for_node_ids(
    connection: asyncpg.Connection, node_ids: list[str], limit: int, offset: int, tenant: str
) -> PaginatedQuestions:
    total = await connection.fetchval(
        """
        SELECT COUNT(DISTINCT q.question_id)
        FROM questions q
        JOIN question_concept_mappings qcm ON qcm.question_id = q.question_id
        WHERE q.tenant_id = $1 AND q.status = 'published' AND qcm.concept_node_id = ANY($2::text[])
        """ + _NOT_UNRELEASED_TEST,
        tenant,
        node_ids,
    )
    rows = await connection.fetch(
        """
        SELECT DISTINCT q.question_id, q.question_type, q.question_text, q.options_json, q.difficulty,
               (SELECT array_agg(m.concept_node_id)
                  FROM question_concept_mappings m
                 WHERE m.question_id = q.question_id) AS concept_ids
        FROM questions q
        JOIN question_concept_mappings qcm ON qcm.question_id = q.question_id
        WHERE q.tenant_id = $1 AND q.status = 'published' AND qcm.concept_node_id = ANY($2::text[])
        """ + _NOT_UNRELEASED_TEST + """
        ORDER BY q.question_id
        LIMIT $3 OFFSET $4
        """,
        tenant,
        node_ids,
        limit,
        offset,
    )
    question_ids = [r["question_id"] for r in rows]
    figures = await _fetch_figures(connection, question_ids)
    return PaginatedQuestions(
        items=[_row_to_question(r, figures) for r in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


async def _fetch_figures(
    connection: asyncpg.Connection, question_ids: list[str]
) -> dict[str, list[QuestionFigure]]:
    if not question_ids:
        return {}
    rows = await connection.fetch(
        """
        SELECT question_id, image_url, placement, option_id, caption
        FROM question_figures
        WHERE question_id = ANY($1::text[])
        ORDER BY question_id, display_order
        """,
        question_ids,
    )
    figures: dict[str, list[QuestionFigure]] = {}
    for r in rows:
        figures.setdefault(r["question_id"], []).append(
            QuestionFigure(
                image_url=r["image_url"],
                placement=r["placement"],
                option_id=r["option_id"],
                caption=r["caption"],
            )
        )
    return figures


def _row_to_question(
    row: asyncpg.Record, figures_by_question: dict[str, list[QuestionFigure]]
) -> Question:
    return Question(
        question_id=row["question_id"],
        question_type=row["question_type"],
        question_text=row["question_text"],
        options=row["options_json"],
        difficulty=row["difficulty"],
        figures=figures_by_question.get(row["question_id"], []),
        concept_ids=list(row["concept_ids"] or []) if "concept_ids" in row else [],
    )
