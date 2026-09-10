import asyncio
import inspect
import os

import pytest

from app.figures import STUDENT_VISIBLE_PLACEMENTS, fetch_figures
from app.routers.content import _build_tree


def _rec(node_id, node_type, title, parent_id, depth, order, description=None):
    return {
        "node_id": node_id,
        "type": node_type,
        "title": title,
        "description": description,
        "parent_id": parent_id,
        "depth": depth,
        "display_order": order,
    }


# --- unit tests: pure tree-building, no database ---


def test_build_tree_nests_children_under_parents():
    rows = [
        _rec("ch", "chapter", "Chapter", None, 0, 0),
        _rec("t1", "topic", "Topic 1", "ch", 1, 0),
        _rec("t2", "topic", "Topic 2", "ch", 1, 1),
        _rec("c1", "concept", "Concept 1", "t1", 2, 0),
    ]
    tree = _build_tree(rows, "ch")
    assert tree.node_id == "ch"
    assert [c.node_id for c in tree.children] == ["t1", "t2"]
    assert [c.node_id for c in tree.children[0].children] == ["c1"]


def test_build_tree_drops_orphans_without_crashing():
    rows = [
        _rec("ch", "chapter", "Chapter", None, 0, 0),
        _rec("x", "topic", "Orphan", "missing_parent", 1, 0),
    ]
    tree = _build_tree(rows, "ch")
    assert tree.children == []


# --- unit tests: a question must never carry the solution's diagram ---


class _RecordingConnection:
    """Just enough asyncpg.Connection to see what SQL a function would run.

    The point is that this needs no database. The rule it guards is a correctness rule,
    so the test for it should never be one of the ones that silently skips.
    """

    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.rows


def test_question_figures_are_restricted_to_what_a_student_may_see():
    """Some questions are answered by the very diagram their solution draws.

    Before this, /questions returned every figure a question owned, including
    placement='explanation', and the apps drew anything without an option_id into the
    stem. A student saw the answer.
    """
    connection = _RecordingConnection()
    asyncio.run(fetch_figures(connection, ["phy_11_ch4_mcq_ncert_q1"]))

    query, args = connection.calls[0]
    assert "placement = ANY" in query, "the query must filter by placement at all"
    assert args[1] == ["stem", "option"]
    assert "explanation" not in args[1]
    assert "ai_explanation" not in args[1]


def test_the_visible_placements_never_grow_to_include_an_answer():
    """A guard on the constant itself, so widening it has to be deliberate."""
    assert set(STUDENT_VISIBLE_PLACEMENTS) == {"stem", "option"}


def test_each_placement_group_is_disjoint_from_the_ones_a_student_sees():
    """The groups are what keep the answer on the far side of the reveal."""
    from app.figures import ALL_PLACEMENTS, AI_EXPLANATION_PLACEMENTS, SOLUTION_PLACEMENTS

    student = set(STUDENT_VISIBLE_PLACEMENTS)
    assert not student & set(SOLUTION_PLACEMENTS)
    assert not student & set(AI_EXPLANATION_PLACEMENTS)
    # ALL_PLACEMENTS is the only one that may overlap, and only the review uses it.
    assert student <= set(ALL_PLACEMENTS)


def test_figures_come_back_grouped_by_option_and_in_display_order():
    """A question with figures on several options must not interleave them."""
    connection = _RecordingConnection()
    asyncio.run(fetch_figures(connection, ["q1"]))
    query = connection.calls[0][0]
    assert "ORDER BY" in query
    assert "option_id" in query.split("ORDER BY")[1]
    assert "display_order" in query.split("ORDER BY")[1]


def test_the_paper_builder_filters_by_allowlist_not_by_naming_one_placement():
    """A denylist here silently reopened once, when a new placement was added.

    The paper a candidate sits must carry stem and option diagrams and nothing else. Written
    as "everything except explanation", adding 'ai_explanation' quietly let a solution's
    diagram back into a live paper.
    """
    import inspect

    from app.routers import tests as tests_router

    source = inspect.getsource(tests_router._paper_for)
    assert "in STUDENT_VISIBLE_PLACEMENTS" in source
    assert 'placement != "explanation"' not in source


def test_a_caller_may_ask_for_the_solutions_figures_explicitly():
    """The reveal endpoint is allowed to see them; it just has to say so."""
    connection = _RecordingConnection()
    asyncio.run(fetch_figures(connection, ["q1"], placements=("explanation",)))
    assert connection.calls[0][1][1] == ["explanation"]


def test_no_question_ids_means_no_query_at_all():
    connection = _RecordingConnection()
    assert asyncio.run(fetch_figures(connection, [])) == {}
    assert connection.calls == []


# --- integration tests: hit the real endpoints against Supabase ---
# Require DATABASE_URL *and* the published phy_11_ch4 seed; skipped otherwise.
#
# The seed check is the second half, and it was added when running against a local
# database became possible at all. These tests assert on the real content — "Laws of
# Motion", 163 questions, particular question ids — so pointing DATABASE_URL at any other
# database made seven of them fail for reasons that had nothing to do with the change
# being tested. Checking for the seed rather than for an environment variable means
# nobody has to remember to set anything: against Supabase they run, elsewhere they skip
# and say why.


def _has_phy_11_ch4_seed() -> bool:
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        return False
    try:
        import asyncio

        import asyncpg

        async def check():
            conn = await asyncpg.connect(dsn)
            try:
                return await conn.fetchval(
                    "SELECT 1 FROM nodes WHERE node_id = 'phy_11_ch4' "
                    "AND status = 'published'"
                )
            finally:
                await conn.close()

        return bool(asyncio.run(check()))
    except Exception:
        return False


integration = pytest.mark.skipif(
    not _has_phy_11_ch4_seed(),
    reason=(
        "needs DATABASE_URL pointing at a database with the published phy_11_ch4 seed; "
        "see db/testdata/seed_plans.sql for the local alternative"
    ),
)


@pytest.fixture(scope="module")
def client():
    from starlette.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def _all_keys(obj, acc):
    if isinstance(obj, dict):
        for k, v in obj.items():
            acc.add(k)
            _all_keys(v, acc)
    elif isinstance(obj, list):
        for item in obj:
            _all_keys(item, acc)
    return acc


@integration
def test_list_chapters_includes_phy_11_ch4_with_subject_name(client):
    r = client.get("/chapters")
    assert r.status_code == 200
    chapters = r.json()
    by_id = {c["node_id"]: c for c in chapters}
    assert "phy_11_ch4" in by_id
    # subject display name is resolved from the subject node, not hardcoded
    assert by_id["phy_11_ch4"]["subject"] == "phy"
    assert by_id["phy_11_ch4"]["subject_name"] == "Physics"


@integration
def test_list_chapters_class_level_filter(client):
    # class 11 physics chapters exist; class 12 has none yet
    r11 = client.get("/chapters?class_level=11")
    assert r11.status_code == 200
    assert all(c["class_level"] == 11 for c in r11.json())
    assert "phy_11_ch4" in [c["node_id"] for c in r11.json()]

    r12 = client.get("/chapters?class_level=12")
    assert r12.status_code == 200
    assert r12.json() == []


@integration
def test_list_chapters_exam_filter(client):
    # neet includes physics (subjects = phy/chem/bio), so physics chapters show
    r_neet = client.get("/chapters?exam=neet")
    assert r_neet.status_code == 200
    assert "phy_11_ch4" in [c["node_id"] for c in r_neet.json()]

    # an unknown/unseeded exam scopes to nothing rather than leaking everything
    r_unknown = client.get("/chapters?exam=does_not_exist")
    assert r_unknown.status_code == 200
    assert r_unknown.json() == []


@integration
def test_chapter_tree_has_expected_shape(client):
    r = client.get("/chapters/phy_11_ch4/tree")
    assert r.status_code == 200
    counts: dict[str, int] = {}

    def walk(node):
        counts[node["type"]] = counts.get(node["type"], 0) + 1
        for child in node["children"]:
            walk(child)

    walk(r.json())
    assert counts == {"chapter": 1, "topic": 6, "subtopic": 14, "concept": 30}


@integration
def test_chapter_questions_paginate_and_never_leak_answers(client):
    r = client.get("/chapters/phy_11_ch4/questions?limit=5")
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 5
    assert body["total"] == 163
    assert body["limit"] == 5 and body["offset"] == 0
    # key-level check: answer fields must appear nowhere in the question payload
    keys = _all_keys(body["items"], set())
    assert "correct_option_ids" not in keys
    assert "explanation_json" not in keys
    assert "explanation" not in keys


@integration
def test_concept_questions_return_tagged_questions(client):
    # a concept known to carry questions
    r = client.get("/concepts/phy_11_ch4_friction_types_static/questions?limit=3")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    assert len(body["items"]) == min(3, body["total"])


@integration
def test_single_question_hides_answer_but_reveal_exposes_it(client):
    q = client.get("/questions/phy_11_ch4_mcq_matching_q3")
    assert q.status_code == 200
    payload = q.json()
    assert "correct_option_ids" not in payload
    assert "explanation" not in payload
    assert len(payload["figures"]) >= 1  # this question carries an R2 figure

    a = client.get("/questions/phy_11_ch4_mcq_matching_q3/answer")
    assert a.status_code == 200
    ans = a.json()
    assert ans["correct_option_ids"]
    assert ans["explanation"]
    assert ans["concepts"]


@integration
@pytest.mark.parametrize(
    "path",
    [
        "/chapters/does_not_exist/tree",
        "/concepts/does_not_exist/questions",
        "/questions/does_not_exist",
        "/questions/does_not_exist/answer",
    ],
)
def test_unknown_ids_return_404(client, path):
    assert client.get(path).status_code == 404


# --- the player page ------------------------------------------------------------

def test_the_player_page_refuses_anything_that_is_not_a_video_id():
    """The id arrives in a query string and is written into a script tag.

    Without this check that is an injection: anyone can hand the app a URL and have
    their own JavaScript served from our origin.
    """
    from app.routers.content import _VIDEO_ID

    assert _VIDEO_ID.match("a2T84FeLIdY")
    assert _VIDEO_ID.match("BX6mVAJQbnE")
    assert not _VIDEO_ID.match('"></script><script>alert(1)</script>')
    assert not _VIDEO_ID.match("short")
    assert not _VIDEO_ID.match("way_too_long_for_an_id")
    assert not _VIDEO_ID.match("has spaces")
    assert not _VIDEO_ID.match("")


def test_the_player_page_carries_the_id_and_a_real_origin():
    from app.routers.content import _PLAYER_PAGE

    page = _PLAYER_PAGE.format(video_id="a2T84FeLIdY", origin="https://example.test")
    assert '"a2T84FeLIdY"' in page
    assert 'origin: "https://example.test"' in page
    # The whole reason this page exists rather than a hand-written iframe.
    assert "https://www.youtube.com/iframe_api" in page
    assert "onError" in page



# --- the exam filter -----------------------------------------------------------------
#
# This cost a whole home screen. The app's canonical exam value is "NEET"
# (Onboarding.kt); the pipeline writes exam_id 'neet'. An exact match returned zero
# chapters for every student who had chosen an exam, and the app — which substitutes
# placeholder subject cards when it has no subjects — showed three convincing cards that
# did nothing at all when tapped.


def test_the_exam_filter_ignores_case_in_the_sql():
    from app.routers import content as content_router

    source = inspect.getsource(content_router.list_chapters)
    assert "lower(e.exam_id) = lower($3)" in source, (
        "an exam id that differs only in case is the same exam"
    )


def test_the_scope_search_matches_the_chapter_list_on_this():
    from app.plans import lookup

    assert "lower(e.exam_id) = lower($7)" in lookup._CANDIDATES_SQL


# --- signed links, exam tokens, and a comparison that used to fall over ---------------


def test_a_notes_link_with_odd_characters_is_refused_rather_than_fatal():
    """The `?t=` on a notes link is a query string: anybody can put anything in it.

    `hmac.compare_digest` raises on two strings holding anything outside ASCII, so one
    accented character turned "that link is not valid" into a 500 — on a route reachable
    by anyone who has ever been sent a URL.
    """
    from app import assets

    for token in ("café", "é" * 64, "🙂", "tok\udce9n"):
        assert assets.verify("notes", "phy_11_ch7", token) is False


def test_a_real_notes_link_still_opens():
    from app import assets

    token = assets.sign("notes", "phy_11_ch7")
    assert assets.verify("notes", "phy_11_ch7", token) is True
    assert assets.verify("notes", "phy_11_ch8", token) is False


def test_an_exam_token_with_odd_characters_is_refused_rather_than_fatal():
    """The browser sitting a paper sends its token in a header, and headers are bytes.

    The same `compare_digest` crash lived here too, on four routes — including submit,
    which is the worst possible place to answer 500: the student is at the end of a timed
    paper with their answers in the request.
    """
    from app.security import constant_time_equals

    for token in ("café", "é" * 43, "🙂", "tok\udce9n", " "):
        assert constant_time_equals(token, "a-real-web-token") is False

    assert constant_time_equals("a-real-web-token", "a-real-web-token") is True
