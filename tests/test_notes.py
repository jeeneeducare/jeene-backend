"""The notes viewer, and the thing it exists to stop.

`chapter_notes.pdf_url` points at an object in storage. That URL is durable,
unauthenticated and shareable, and the table has carried a comment since it was written
saying the reader would be served through our own viewer rather than handed the file.
These tests are that promise, written down: the metadata response must not contain the
storage URL, and a link that does work must stop working.
"""

import os
import time

import pytest
from fastapi.testclient import TestClient

from app import assets
from app.schemas import ChapterNotes

integration = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; these need a real database",
)

CHAPTER = "phy_11_ch8"    # Gravitation, the only chapter the seed gives notes


@pytest.fixture(scope="module")
def client():
    from app.auth import current_tenant, optional_user, require_user
    from app.main import app

    app.dependency_overrides[require_user] = lambda: {"uid": "student-fresh"}
    app.dependency_overrides[optional_user] = lambda: {"uid": "student-fresh"}
    app.dependency_overrides[current_tenant] = lambda: "JEENE_MASTER"
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


# --- the response model ---------------------------------------------------------------

def test_notes_survive_a_row_that_never_recorded_its_size():
    """`page_count` and `size_bytes` are nullable columns, so the model must allow null.

    They were declared required. A published row whose size had not been recorded made
    `GET /chapters/{id}/notes` raise a validation error and answer 500 — and because
    nothing had ever called the endpoint, it stayed that way until a plan step pointed at
    notes and the app opened it.
    """
    notes = ChapterNotes(
        chapter_id=CHAPTER,
        title="Gravitation — chapter notes",
        viewer_url="https://example.test/notes/phy_11_ch8?t=abc",
        page_count=14,
        size_bytes=None,
    )
    assert notes.size_bytes is None
    assert notes.page_count == 14


def test_notes_still_require_somewhere_to_send_the_reader():
    """Nullable is not the same as optional everywhere. Without somewhere to open, a row
    like this is a content bug worth failing loudly on."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ChapterNotes(chapter_id=CHAPTER, title="Gravitation", page_count=14)


# --- signing --------------------------------------------------------------------------

def test_a_token_opens_only_the_thing_it_was_minted_for():
    token = assets.sign("notes", CHAPTER)

    assert assets.verify("notes", CHAPTER, token)
    # Another chapter's notes.
    assert not assets.verify("notes", "phy_11_ch5", token)
    # The same id, a different kind of asset. Without `kind` in the digest this passes,
    # and a notes link becomes a link to whatever else is keyed by a chapter id.
    assert not assets.verify("video", CHAPTER, token)


def test_a_token_stops_working():
    expired = assets.sign("notes", CHAPTER, ttl_seconds=-1)
    assert not assets.verify("notes", CHAPTER, expired)


def test_rubbish_is_refused_rather_than_raising():
    for token in ["", "nonsense", ".", "abc.def", "9999999999.", ".deadbeef",
                  "not-a-number.deadbeef"]:
        assert not assets.verify("notes", CHAPTER, token), token


def test_an_expiry_cannot_be_moved_without_the_secret():
    """The expiry is in the clear so it can be read without the secret — which means the
    digest has to cover it, or anybody can extend their own link."""
    token = assets.sign("notes", CHAPTER)
    _, _, digest = token.partition(".")
    forged = f"{int(time.time()) + 86400}.{digest}"
    assert not assets.verify("notes", CHAPTER, forged)


def _stored_pdf_url() -> str:
    """Where this chapter's notes actually live, straight from the table."""
    import asyncio

    import asyncpg

    async def read() -> str:
        conn = await asyncpg.connect(os.environ["DATABASE_URL"])
        try:
            return await conn.fetchval(
                "SELECT pdf_url FROM chapter_notes WHERE chapter_id = $1", CHAPTER
            )
        finally:
            await conn.close()

    return asyncio.run(read())


# --- the routes -----------------------------------------------------------------------

@integration
def test_the_storage_url_never_reaches_the_client(client):
    """The whole point of the ticket, in one assertion."""
    response = client.get(f"/chapters/{CHAPTER}/notes")
    assert response.status_code == 200
    body = response.json()

    assert "pdf_url" not in body
    # Whatever storage host the row happens to name, none of it is in the response.
    assert "cdn.example" not in response.text
    assert ".pdf" not in response.text
    assert f"/notes/{CHAPTER}?t=" in body["viewer_url"]


@integration
def test_the_viewer_and_the_file_both_refuse_an_unsigned_link(client):
    assert client.get(f"/notes/{CHAPTER}").status_code == 403
    assert client.get(f"/notes/{CHAPTER}", params={"t": "nonsense"}).status_code == 403
    assert client.get(f"/notes/{CHAPTER}/file").status_code == 403
    assert client.get(
        f"/notes/{CHAPTER}/file", params={"t": "nonsense"}
    ).status_code == 403


@integration
def test_the_viewer_page_points_at_our_own_file_route(client):
    token = client.get(f"/chapters/{CHAPTER}/notes").json()["viewer_url"]
    # The URL is absolute; TestClient wants the path.
    path = token.split("://", 1)[1].split("/", 1)[1]
    page = client.get("/" + path)
    assert page.status_code == 200
    assert f"/notes/{CHAPTER}/file?t=" in page.text
    # Same guarantee as the metadata: the reader page does not name storage either.
    assert "cdn.example" not in page.text


@integration
def test_a_chapter_without_notes_is_a_404_not_a_signed_link_to_nothing(client):
    assert client.get("/chapters/phy_11_ch5/notes").status_code == 404
    signed = assets.sign("notes", "phy_11_ch5")
    assert client.get("/notes/phy_11_ch5", params={"t": signed}).status_code == 404


@integration
def test_the_file_route_streams_what_storage_returns(client, monkeypatch):
    """The bytes go through us. Stubbed at the transport so this proves the streaming
    path itself — the request we make, the type we answer with, the body we pass on —
    without depending on the fixture's URL resolving anywhere."""
    import httpx

    from app.routers import notes as notes_router

    body = b"%PDF-1.4\n" + b"x" * 4096
    asked: list[str] = []

    def storage(request: httpx.Request) -> httpx.Response:
        asked.append(str(request.url))
        return httpx.Response(200, content=body, headers={"content-length": str(len(body))})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        notes_router.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(storage), **kw),
    )

    token = assets.sign("notes", CHAPTER)
    response = client.get(f"/notes/{CHAPTER}/file", params={"t": token})

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.content == body
    # It went to the row's storage URL — read from the row rather than written here, so
    # the test says "wherever the notes actually live" instead of restating the seed.
    assert asked == [_stored_pdf_url()]
    # And that URL is the thing the client is never told.
    assert asked[0] not in client.get(f"/chapters/{CHAPTER}/notes").text


@integration
def test_storage_being_down_is_a_502_not_a_traceback(client, monkeypatch):
    import httpx

    from app.routers import notes as notes_router

    def storage(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to storage", request=request)

    real = httpx.AsyncClient
    monkeypatch.setattr(
        notes_router.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(storage), **kw),
    )

    token = assets.sign("notes", CHAPTER)
    response = client.get(f"/notes/{CHAPTER}/file", params={"t": token})

    assert response.status_code == 502
    # Nothing about where the file lives, even when the thing that broke is where it lives.
    assert "cdn.example" not in response.text


# --- the renderer ---------------------------------------------------------------------

@integration
def test_the_renderer_is_ours_and_only_the_two_files_are(client):
    for name in ("pdf.min.js", "pdf.worker.min.js"):
        served = client.get(f"/notes-viewer/{name}")
        assert served.status_code == 200, name
        assert len(served.content) > 100_000, name

    # Anything else, including a walk out of the directory.
    assert client.get("/notes-viewer/anything.js").status_code == 404
    assert client.get("/notes-viewer/..%2f..%2fconfig.py").status_code == 404


@integration
def test_the_viewer_page_loads_the_renderer_from_us(client):
    token = assets.sign("notes", CHAPTER)
    page = client.get(f"/notes/{CHAPTER}", params={"t": token}).text

    assert "/notes-viewer/pdf.min.js" in page
    assert "/notes-viewer/pdf.worker.min.js" in page
    # The reader must not need a host other than this one: the file it is about to open
    # is streamed from here, so a CDN could only ever be one more thing to be down.
    assert "cdnjs" not in page
    assert "//unpkg" not in page


@integration
def test_the_viewer_page_does_not_declare_a_global_named_status(client):
    """`var status` at the top level of a page is `window.status`, which is a string.

    Assigning an element to it coerces, every `.textContent` write is dropped, and a page
    that has failed shows its loading text forever instead of the error. That is how the
    first version of this viewer hid a broken script tag.
    """
    import re

    token = assets.sign("notes", CHAPTER)
    page = client.get(f"/notes/{CHAPTER}", params={"t": token}).text
    # The page explains this trap in a comment, so strip comments before looking — a test
    # that fails on the warning about a bug rather than on the bug is worse than useless.
    code = re.sub(r"//.*", "", page)

    assert not re.search(r"\b(var|let|const)\s+status\b", code)
