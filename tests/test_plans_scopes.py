"""Finding a scope from what a student typed.

Two halves worth testing without a database. The first is the normalising, which is pure
text work and where the whole feature's behaviour actually lives: whether "i want to
study kinematics" finds *Kinematics* is decided here, not in SQL. The second is the set
of guarantees the SQL has to keep — tenant scoping, published only, nothing without
questions, no value ever interpolated — because each is a rule that a later edit could
drop silently and no unit test would notice.

The ranking itself is checked against a real database in `test_plans_integration.py`.
"""

import asyncio
import inspect

import pytest

from app.plans import lookup
from app.routers import plans as plans_router


def run(coro):
    return asyncio.run(coro)


class _FakeConnection:
    """Returns queued result sets, one per `fetch`, and remembers what it was asked."""

    def __init__(self, *result_sets):
        self.queued = list(result_sets)
        self.calls = []

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        return self.queued.pop(0) if self.queued else []


# --- normalising ---------------------------------------------------------------------


def test_plain_chapter_name_is_its_own_raw_and_cleaned_form():
    raw, cleaned, tokens = lookup.normalise("Thermodynamics")
    assert raw == "thermodynamics"
    assert cleaned == "thermodynamics"
    assert tokens == ["thermodynamics"]


def test_a_whole_sentence_cleans_down_to_the_chapter():
    raw, cleaned, tokens = lookup.normalise("I want to study Kinematics please")
    assert raw == "i want to study kinematics please"
    assert cleaned == "kinematics"
    assert tokens == ["kinematics"]


def test_raw_keeps_the_articles_a_real_title_contains():
    # The reason two forms exist: this title *is* mostly stop-words, and cleaning alone
    # would never match it.
    raw, cleaned, _ = lookup.normalise("Motion in a Straight Line")
    assert raw == "motion in a straight line"
    assert cleaned == "motion straight line"


def test_punctuation_becomes_separation_not_characters():
    raw, _, tokens = lookup.normalise("p-Block  Elements!")
    assert raw == "p block elements"
    assert tokens == ["block", "elements"]  # "p" is below the one-character floor


def test_like_wildcards_do_not_survive_normalising():
    # They are stripped here; the SQL also avoids LIKE entirely. Belt and braces, and
    # this is the belt.
    raw, cleaned, tokens = lookup.normalise("100%_motion")
    assert "%" not in raw and "_" not in raw
    assert "%" not in cleaned and "_" not in cleaned
    assert all("%" not in t and "_" not in t for t in tokens)
    assert tokens == ["100", "motion"]


def test_nothing_but_asking_words_yields_no_tokens():
    _, cleaned, tokens = lookup.normalise("i want to study the chapter")
    assert cleaned == ""
    assert tokens == []


@pytest.mark.parametrize("text", ["", "   ", "!!!", None])
def test_empty_input_is_empty_output(text):
    assert lookup.normalise(text) == ("", "", [])


def test_the_query_is_bounded_in_length_and_token_count():
    raw, _, tokens = lookup.normalise("gravitation " * 400)
    assert len(raw) <= lookup.MAX_QUERY_CHARS
    assert len(tokens) <= lookup.MAX_TOKENS


def test_single_characters_are_dropped_from_tokens():
    _, _, tokens = lookup.normalise("s p q gravitation")
    assert tokens == ["gravitation"]


# --- the search ----------------------------------------------------------------------


def test_a_query_with_no_content_words_never_reaches_the_database():
    # Not just an optimisation: an empty token array makes the all-tokens branch
    # (`NOT EXISTS` over nothing) vacuously true, which would score every node in the
    # syllabus as a match.
    conn = _FakeConnection()
    assert run(lookup.search_scopes(conn, "T", "i want to study")) == []
    assert conn.calls == []


def test_the_tenant_and_the_two_phrases_are_arguments_not_text():
    conn = _FakeConnection()
    run(lookup.search_scopes(conn, "JEENE_MASTER", "I want to study Kinematics"))
    query, args = conn.calls[0]
    assert args[0] == "JEENE_MASTER"
    assert args[1] == "i want to study kinematics"   # raw
    assert args[2] == "kinematics"                   # cleaned
    assert args[3] == ["kinematics"]                 # tokens
    # Nothing the student typed appears in the SQL itself.
    assert "kinematics" not in query.lower()


def test_the_limit_is_clamped_and_over_fetched_before_filtering():
    conn = _FakeConnection()
    run(lookup.search_scopes(conn, "T", "gravitation", limit=999))
    _, args = conn.calls[0]
    assert args[-1] == lookup.MAX_LIMIT
    assert args[-2] == lookup.MAX_LIMIT * lookup._CANDIDATE_MULTIPLIER


def test_only_scope_types_are_offered():
    conn = _FakeConnection()
    run(lookup.search_scopes(conn, "T", "gravitation"))
    _, args = conn.calls[0]
    assert set(args[4]) == {"chapter", "topic", "subtopic"}


def test_a_result_carries_the_chapter_and_subject_the_app_needs():
    conn = _FakeConnection(
        [{"node_id": "phy_11_ch8_s1", "type": "subtopic", "title": "Acceleration due to gravity",
          "subject_id": "phy", "class_level": 11, "score": lookup.EXACT, "question_count": 48}],
        [{"root_id": "phy_11_ch8_s1", "chapter_node_id": "phy_11_ch8",
          "chapter_title": "Gravitation"}],
        [{"subject_id": "phy", "title": "Physics"}],
    )
    [match] = run(lookup.search_scopes(conn, "T", "acceleration due to gravity"))
    # chapter_node_id is what beginIntake reads the student's record against.
    assert match["chapter_node_id"] == "phy_11_ch8"
    assert match["chapter_title"] == "Gravitation"
    assert match["subject_name"] == "Physics"
    assert match["exact"] is True


def test_anything_short_of_an_exact_title_is_not_marked_exact():
    conn = _FakeConnection(
        [{"node_id": "n", "type": "chapter", "title": "Gravitation", "subject_id": "phy",
          "class_level": 11, "score": lookup.EXACT - 1, "question_count": 3}],
        [], [],
    )
    [match] = run(lookup.search_scopes(conn, "T", "gravit"))
    assert match["exact"] is False


def test_no_candidates_means_no_further_queries():
    conn = _FakeConnection([])
    assert run(lookup.search_scopes(conn, "T", "quidditch")) == []
    assert len(conn.calls) == 1


# --- the guarantees the SQL has to keep ----------------------------------------------


def test_no_value_is_ever_formatted_into_the_sql():
    # The f-strings in this module interpolate score constants and two fixed SQL
    # fragments. Nothing derived from a request may join them, so every placeholder in
    # the source is checked to be one of those known names.
    source = inspect.getsource(lookup)
    allowed = {
        "_NORM_TITLE", "NOT_UNRELEASED_TEST_SQL",
        "EXACT", "_PREFIX", "_PHRASE", "_ALL_TOKENS", "_SOME_TOKENS", "_KEYWORD",
    }
    import re

    for placeholder in re.findall(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", source):
        assert placeholder in allowed, f"{placeholder} is interpolated into SQL"


def test_every_query_is_scoped_to_the_tenant():
    for sql in (lookup._CANDIDATES_SQL, lookup._CONTEXT_SQL, lookup._SUBJECT_SQL):
        assert "tenant_id = $1" in sql


def test_only_published_content_is_searched():
    assert lookup._CANDIDATES_SQL.count("status = 'published'") >= 3


def test_a_scope_with_nothing_to_practise_is_dropped():
    # An inner join to the counts CTE, not a left join: a node that produced no count
    # has no published questions under it, and create_plan would refuse it with a 409.
    assert "JOIN counts k ON k.root_id = c.node_id" in lookup._CANDIDATES_SQL
    assert "LEFT JOIN counts" not in lookup._CANDIDATES_SQL


def test_only_concept_mapped_questions_count_toward_plannability():
    # The planner counts questions mapped to the scope's *concepts* — resolve_scope takes
    # the concept-typed subtree and the inventory's buckets come from those alone. The
    # mapping column is only REFERENCES nodes(node_id), so a row pointing at a subtopic
    # is schema-legal; counting it here would promise a scope whose buckets are empty.
    assert "WHERE d.type = 'concept'" in lookup._CANDIDATES_SQL


def test_an_unreleased_test_paper_does_not_make_a_scope_look_plannable():
    assert "test_questions" in lookup._CANDIDATES_SQL
    assert "released_at IS NOT NULL" in lookup._CANDIDATES_SQL


def test_the_search_does_not_use_like_at_all():
    # position() and left() cannot read a wildcard, so a query that somehow kept a `%`
    # would still be matched literally.
    assert " LIKE " not in lookup._CANDIDATES_SQL.upper()


# --- the route -----------------------------------------------------------------------


def test_scopes_is_declared_before_the_plan_id_route():
    # FastAPI matches in declaration order. Below `/{plan_id}`, "scopes" is a plan id.
    paths = [
        r.path for r in plans_router.router.routes if "GET" in getattr(r, "methods", ())
    ]
    assert paths.index("/plans/scopes") < paths.index("/plans/{plan_id}")


def test_the_route_requires_a_signed_in_student():
    signature = inspect.signature(plans_router.search_scopes)
    dependencies = [
        p.default.dependency
        for p in signature.parameters.values()
        if hasattr(p.default, "dependency")
    ]
    from app.auth import require_user

    assert require_user in dependencies
