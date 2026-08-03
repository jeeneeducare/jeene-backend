from app.routers.admin import _as_id


# --- what people actually paste ---------------------------------------------------

def test_every_url_shape_a_browser_hands_you():
    """An admin pastes whatever was in the address bar. All of these are the same video."""
    for pasted in [
        "a2T84FeLIdY",
        "https://www.youtube.com/watch?v=a2T84FeLIdY",
        "https://www.youtube.com/watch?v=a2T84FeLIdY&t=42s",
        "https://youtu.be/a2T84FeLIdY",
        "https://youtu.be/a2T84FeLIdY?si=abcdef",
        "https://www.youtube.com/embed/a2T84FeLIdY",
        "https://www.youtube.com/live/a2T84FeLIdY",
        "https://m.youtube.com/watch?v=a2T84FeLIdY",
        "  https://www.youtube.com/watch?v=a2T84FeLIdY  ",
        "www.youtube.com/watch?v=a2T84FeLIdY",
    ]:
        assert _as_id(pasted) == "a2T84FeLIdY", pasted


def test_anything_that_is_not_a_video_is_refused():
    """Refusing here is what stops a typo becoming a tile that opens nothing."""
    for junk in [
        "",
        "   ",
        "https://www.youtube.com/",
        "https://www.youtube.com/@CompetitionWallah",
        "https://www.youtube.com/playlist?list=PLabc",
        "https://example.com/watch?v=a2T84FeLIdY_toolong",
        "not a link at all",
        "short",
        "<script>alert(1)</script>",
    ]:
        assert _as_id(junk) is None, junk


def test_a_playlist_link_does_not_smuggle_its_first_video_through():
    # A playlist URL carries no `v`, and taking the last path segment would give "playlist".
    assert _as_id("https://www.youtube.com/playlist?list=PLPzsoc1Ia7okl") is None
