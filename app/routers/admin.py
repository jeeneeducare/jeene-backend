"""The admin panel's API: attaching videos to the content tree.

Everything here writes content, so everything here is behind `require_admin`. That is a
row in a table rather than a token claim, because a valid Firebase token proves only that
somebody has opened the app once, which is true of every student.

Three rules the panel relies on and this enforces, rather than trusting the browser:

  * a video is checked against YouTube before it is stored, the same way the pipeline
    script checks it, so a typo cannot become a tile that opens nothing;
  * a node id has to exist in this tenant's tree, so a link cannot be hung on nothing;
  * a video is stored at draft unless it is explicitly published, exactly like every other
    kind of content, so a paste is never immediately in front of a student.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

import asyncpg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import get_connection
from app.auth import require_admin
from app.schemas import AdminNode, AdminVideo, ChapterSummary

router = APIRouter(prefix="/admin", tags=["admin"])

_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
OEMBED = "https://www.youtube.com/oembed"


def _as_id(value: str) -> str | None:
    """The video id, whether given bare or inside any URL a person might paste."""
    value = (value or "").strip()
    if not value:
        return None
    if "/" not in value and "?" not in value:
        return value if _VIDEO_ID.match(value) else None

    parsed = urllib.parse.urlparse(value if "//" in value else f"https://{value}")
    if parsed.netloc.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/")
    elif "watch" in parsed.path:
        candidate = urllib.parse.parse_qs(parsed.query).get("v", [""])[0]
    else:
        candidate = parsed.path.rstrip("/").split("/")[-1]
    return candidate if _VIDEO_ID.match(candidate) else None


def _describe(video_id: str) -> dict | None:
    """What YouTube says this video is. None means it does not serve it.

    oEmbed, so no key and no quota. The title comes from here rather than from whoever
    pasted the link, so what a student reads is what they will see when it opens.
    """
    url = f"{OEMBED}?" + urllib.parse.urlencode({
        "url": f"https://www.youtube.com/watch?v={video_id}", "format": "json",
    })
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            return json.loads(response.read())
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError):
        return None


def _embeddable(video_id: str) -> bool:
    """Whether its owner lets it play outside YouTube.

    A different question from "does it exist", and the one that decides whether the tile
    opens a player or a grey box. A page we cannot read at all gets the benefit of the
    doubt rather than blocking a video that is probably fine.
    """
    request = urllib.request.Request(
        f"https://www.youtube.com/watch?v={video_id}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return '"playableInEmbed":false' not in response.read().decode("utf-8", "replace")
    except (urllib.error.HTTPError, urllib.error.URLError):
        return True


@router.get("/me")
async def whoami(admin: dict = Depends(require_admin)) -> dict:
    """Confirms the caller is an administrator. The panel's front door."""
    return {"uid": admin["uid"], "email": admin.get("admin_email", ""),
            "tenant_id": admin["tenant_id"]}


@router.get("/chapters", response_model=list[ChapterSummary])
async def chapters(
    admin: dict = Depends(require_admin),
    connection: asyncpg.Connection = Depends(get_connection),
) -> list[ChapterSummary]:
    """Every chapter, published or not, so the panel can reach content before release."""
    rows = await connection.fetch(
        """
        SELECT c.node_id, c.title, c.subject_id, c.class_level, c.status,
               coalesce(s.title, c.subject_id) AS subject_title,
               (SELECT count(*) FROM node_videos v
                 JOIN nodes n ON n.node_id = v.node_id
                WHERE v.tenant_id = c.tenant_id
                  AND (n.node_id = c.node_id OR n.parent_id = c.node_id
                       OR n.parent_id IN (SELECT node_id FROM nodes WHERE parent_id = c.node_id)
                       OR n.parent_id IN (SELECT node_id FROM nodes
                                           WHERE parent_id IN (SELECT node_id FROM nodes
                                                                WHERE parent_id = c.node_id)))
               ) AS video_count
          FROM nodes c
          LEFT JOIN nodes s ON s.node_id = c.subject_id
         WHERE c.tenant_id = $1 AND c.type = 'chapter'
         ORDER BY c.class_level, c.subject_id, c.ncert_chapter_number NULLS LAST, c.title
        """,
        admin["tenant_id"],
    )
    return [ChapterSummary(**dict(r)) for r in rows]


@router.get("/chapters/{chapter_id}/tree", response_model=AdminNode)
async def tree(
    chapter_id: str,
    admin: dict = Depends(require_admin),
    connection: asyncpg.Connection = Depends(get_connection),
) -> AdminNode:
    """The chapter's whole tree with whatever is already attached at every level.

    One query for the nodes and one for the videos rather than a query per node: a
    chapter runs to a couple of hundred nodes and the panel redraws this after every
    change.
    """
    rows = await connection.fetch(
        """
        WITH RECURSIVE branch AS (
            SELECT node_id, parent_id, type, title, display_order, 0 AS depth
              FROM nodes WHERE node_id = $1 AND tenant_id = $2
            UNION ALL
            SELECT n.node_id, n.parent_id, n.type, n.title, n.display_order, b.depth + 1
              FROM nodes n JOIN branch b ON n.parent_id = b.node_id
             WHERE n.tenant_id = $2
        )
        SELECT * FROM branch ORDER BY depth, display_order, title
        """,
        chapter_id, admin["tenant_id"],
    )
    if not rows:
        raise HTTPException(status_code=404, detail=f"No chapter '{chapter_id}'")

    videos = await connection.fetch(
        """
        SELECT node_id, youtube_id, title, channel, thumbnail_url, position, status,
               added_by, added_at
          FROM node_videos
         WHERE tenant_id = $1 AND node_id = ANY($2::text[])
         ORDER BY position, added_at
        """,
        admin["tenant_id"], [r["node_id"] for r in rows],
    )
    attached: dict[str, list[AdminVideo]] = {}
    for v in videos:
        attached.setdefault(v["node_id"], []).append(AdminVideo(**dict(v)))

    built = {
        r["node_id"]: AdminNode(
            node_id=r["node_id"], type=r["type"], title=r["title"],
            videos=attached.get(r["node_id"], []), children=[],
        )
        for r in rows
    }
    for r in rows:
        if r["parent_id"] in built and r["node_id"] != chapter_id:
            built[r["parent_id"]].children.append(built[r["node_id"]])
    return built[chapter_id]


class AttachRequest(BaseModel):
    node_id: str
    # A link or a bare id; people paste whatever their browser gave them.
    url: str
    publish: bool = False


@router.post("/videos", response_model=AdminVideo)
async def attach(
    body: AttachRequest,
    admin: dict = Depends(require_admin),
    connection: asyncpg.Connection = Depends(get_connection),
) -> AdminVideo:
    """Attach a video to any node in the tree.

    Every check that the pipeline script makes is made here too, because the panel is now
    the easier way in and a rule enforced in only one of two doors is not a rule.
    """
    video_id = _as_id(body.url)
    if video_id is None:
        raise HTTPException(status_code=400,
                            detail="That is not a YouTube link or video id")

    node = await connection.fetchrow(
        "SELECT node_id, title, type FROM nodes WHERE node_id = $1 AND tenant_id = $2",
        body.node_id, admin["tenant_id"],
    )
    if node is None:
        raise HTTPException(status_code=404, detail="No such place in the tree")

    meta = _describe(video_id)
    if meta is None:
        raise HTTPException(status_code=400,
                            detail="YouTube does not serve that video")
    if not _embeddable(video_id):
        raise HTTPException(
            status_code=400,
            detail="Its owner does not allow it to play outside YouTube, so it would "
                   "open a dead player in the app",
        )

    # Appended, so the order on screen is the order somebody put them in.
    position = await connection.fetchval(
        "SELECT coalesce(max(position), 0) + 1 FROM node_videos "
        "WHERE node_id = $1 AND tenant_id = $2",
        body.node_id, admin["tenant_id"],
    )
    row = await connection.fetchrow(
        """
        INSERT INTO node_videos
          (node_id, youtube_id, tenant_id, title, channel, thumbnail_url,
           position, status, added_by)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (node_id, youtube_id) DO UPDATE SET
          title = EXCLUDED.title, channel = EXCLUDED.channel,
          thumbnail_url = EXCLUDED.thumbnail_url, status = EXCLUDED.status,
          added_by = EXCLUDED.added_by
        RETURNING node_id, youtube_id, title, channel, thumbnail_url, position, status,
                  added_by, added_at
        """,
        body.node_id, video_id, admin["tenant_id"],
        (meta.get("title") or video_id).strip(),
        (meta.get("author_name") or "").strip(),
        meta.get("thumbnail_url") or "",
        position,
        "published" if body.publish else "draft",
        admin.get("admin_email") or admin["uid"],
    )
    return AdminVideo(**dict(row))


class StatusRequest(BaseModel):
    publish: bool


@router.patch("/videos/{node_id}/{youtube_id}", response_model=AdminVideo)
async def set_status(
    node_id: str,
    youtube_id: str,
    body: StatusRequest,
    admin: dict = Depends(require_admin),
    connection: asyncpg.Connection = Depends(get_connection),
) -> AdminVideo:
    """Publish or withdraw one video. Withdrawing is how a mistake is undone quickly."""
    row = await connection.fetchrow(
        """
        UPDATE node_videos SET status = $4
         WHERE node_id = $1 AND youtube_id = $2 AND tenant_id = $3
        RETURNING node_id, youtube_id, title, channel, thumbnail_url, position, status,
                  added_by, added_at
        """,
        node_id, youtube_id, admin["tenant_id"],
        "published" if body.publish else "draft",
    )
    if row is None:
        raise HTTPException(status_code=404, detail="No such video here")
    return AdminVideo(**dict(row))


@router.delete("/videos/{node_id}/{youtube_id}")
async def detach(
    node_id: str,
    youtube_id: str,
    admin: dict = Depends(require_admin),
    connection: asyncpg.Connection = Depends(get_connection),
) -> dict:
    removed = await connection.execute(
        "DELETE FROM node_videos WHERE node_id = $1 AND youtube_id = $2 AND tenant_id = $3",
        node_id, youtube_id, admin["tenant_id"],
    )
    if removed.endswith("0"):
        raise HTTPException(status_code=404, detail="No such video here")
    return {"removed": True}
