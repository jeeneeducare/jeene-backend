import os

import pytest

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


# --- integration tests: hit the real endpoints against Supabase ---
# Require DATABASE_URL and the published phy_11_ch4 seed; skipped otherwise.

integration = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; integration tests need Supabase",
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
def test_list_chapters_includes_phy_11_ch4(client):
    r = client.get("/chapters")
    assert r.status_code == 200
    assert "phy_11_ch4" in [c["node_id"] for c in r.json()]


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
