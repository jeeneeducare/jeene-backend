"""The study planner's boundary, and the shapes either side of it.

The first test in this file is the reason the file exists. Everything a planning model
learns about the question bank comes out of `app/plans/`, and the guarantee that it never
learns the bank's *contents* is a property of the SQL there. So it is asserted against the
SQL, without a database, in a test that can never silently skip — the mistake this guards
against is a future field added in a hurry, and a test that only runs when someone has
DATABASE_URL set would not be there to stop it.
"""

import asyncio
import inspect
import re

import pytest

from app.plans import inventory as inventory_module
from app.plans import record as record_module
from app.plans import scope as scope_module
from app.plans.inventory import (
    FORBIDDEN_COLUMNS,
    _MAX_BUCKETS,
    _TARGET_DIFFICULTY,
    _roll_up,
    build_inventory,
)
from app.plans.schema import Inventory, QuestionBucket
from app.plans.scope import SCOPE_TYPES, ResolvedScope

PLAN_MODULES = (inventory_module, record_module, scope_module)


def _sql_literals(module) -> dict[str, str]:
    """Every module-level string that looks like SQL, by name.

    Dunders are excluded because the module docstrings talk *about* SQL, and a docstring
    quoting `SELECT *` to explain why it is banned should not fail the ban.
    """
    return {
        name: value
        for name, value in vars(module).items()
        if isinstance(value, str)
        and not name.startswith("__")
        and re.search(r"\bSELECT\b", value, re.IGNORECASE)
    }


def _sql_constants(module) -> dict[str, str]:
    """Only the queries this module actually declares.

    Narrower than `_sql_literals` on purpose: an imported fragment like
    NOT_UNRELEASED_TEST_SQL carries no parameters of its own and is checked where it is
    interpolated, not where it is imported.
    """
    source = inspect.getsource(module)
    return {
        name: value
        for name, value in _sql_literals(module).items()
        if name.endswith("_SQL") and f"{name} = " in source
    }


# --- the boundary ----------------------------------------------------------------


def test_inventory_never_carries_content():
    """No query that feeds a model may read a column that holds an answer.

    A planner is sent a catalogue: what exists, how much of it, how the student has done.
    Send it `question_text` and the feature stops being a planner and becomes a way to
    read the bank out through a third party — and no amount of prompt wording takes that
    back, because by then the content has already left.
    """
    offences = []
    for module in PLAN_MODULES:
        for name, sql in _sql_literals(module).items():
            lowered = sql.lower()
            for column in FORBIDDEN_COLUMNS:
                if column in lowered:
                    offences.append(f"{module.__name__}.{name} reads {column}")
    assert not offences, "content columns reached the planner: " + "; ".join(offences)


def test_no_question_id_is_ever_selected_for_the_model():
    """The planner picks a filter, never a question.

    It cannot name a question that has been edited, deleted or was never there, because
    it is never given a name to use. `resolve.py` turns filters into questions on this
    side of the boundary.
    """
    payload_fields = set(Inventory.model_json_schema()["$defs"]) | set(
        Inventory.model_fields
    )
    assert not any("question_id" in f for f in payload_fields)

    for field in QuestionBucket.model_fields:
        assert field != "question_id"
        assert not field.endswith("question_ids")


def test_the_forbidden_list_covers_every_answer_bearing_column():
    """A guard on the list itself, so shrinking it has to be deliberate.

    These are the columns that carry, or are part of, an answer. `figures.py` learned the
    hard way that a worked solution's diagram is as much the answer as its text.
    """
    for column in (
        "question_text",
        "options_json",
        "correct_option_ids",
        "explanation_json",
        "numerical_answer",
        "assertion_text",
        "reasoning_text",
        "image_url",
        "pdf_url",
    ):
        assert column in FORBIDDEN_COLUMNS, column


def test_the_check_actually_fails_when_a_content_column_is_added():
    """The guard above is only worth having if it would catch the mistake it names.

    Written because a test that reads source can pass for the wrong reason — a changed
    module path, a renamed constant — and would then wave through exactly what it exists
    to stop.
    """
    doctored = """
        SELECT q.question_id, q.question_text, q.correct_option_ids
          FROM questions q
    """
    hits = [c for c in FORBIDDEN_COLUMNS if c in doctored.lower()]
    assert "question_text" in hits and "correct_option_ids" in hits


def test_no_select_star_in_anything_that_feeds_a_model():
    """`SELECT *` would let a new column widen the payload without anyone deciding to."""
    for module in PLAN_MODULES:
        for name, sql in _sql_literals(module).items():
            assert "select *" not in sql.lower(), f"{module.__name__}.{name}"
            assert not re.search(r"select\s+\w+\.\*", sql.lower()), (
                f"{module.__name__}.{name}"
            )


def test_every_query_is_parameterised_and_tenant_scoped():
    """Rules 4 and 6 of CLAUDE.md, checked rather than remembered."""
    for module in PLAN_MODULES:
        for name, sql in _sql_constants(module).items():
            assert "%s" not in sql, f"{module.__name__}.{name} interpolates"
            assert "$1" in sql, f"{module.__name__}.{name} takes no parameters"
            if "FROM nodes" in sql or "FROM questions" in sql:
                assert "tenant_id" in sql, f"{module.__name__}.{name} is not scoped"


def test_the_inventory_model_exposes_no_free_text_from_a_question():
    """A field-level read of the contract, so a new field has to argue for itself."""
    schema = Inventory.model_json_schema()
    names = set()
    for definition in schema.get("$defs", {}).values():
        names |= set(definition.get("properties", {}))
    names |= set(schema.get("properties", {}))
    for suspicious in ("text", "explanation", "options", "answer", "solution", "url"):
        assert suspicious not in names, suspicious


# --- a fake connection, so the rest needs no database -----------------------------


class _FakeConnection:
    """Enough asyncpg.Connection to drive `build_inventory` with canned rows.

    Queries are matched on a distinctive fragment rather than in call order, so the test
    does not break the first time the builder reorders its work.
    """

    def __init__(self, by_fragment=None, values=None):
        self.by_fragment = by_fragment or {}
        self.values = values or {}
        self.queries = []

    def _rows_for(self, query):
        self.queries.append(query)
        for fragment, rows in self.by_fragment.items():
            if fragment in query:
                return rows
        return []

    async def fetch(self, query, *args):
        return self._rows_for(query)

    async def fetchrow(self, query, *args):
        rows = self._rows_for(query)
        return rows[0] if rows else None

    async def fetchval(self, query, *args):
        self.queries.append(query)
        for fragment, value in self.values.items():
            if fragment in query:
                return value
        return 0


def _scope():
    return ResolvedScope(
        node={
            "node_id": "phy_11_ch8_g_variation",
            "type": "subtopic",
            "title": "Acceleration due to gravity",
            "description": "How g varies with height and depth",
            "subject_id": "phy",
            "class_level": 11,
            "estimated_minutes": 45,
            "difficulty": "medium",
            "pedagogical_notes": None,
            "parent_id": "phy_11_ch8_t1",
        },
        chapter={
            "node_id": "phy_11_ch8",
            "title": "Gravitation",
            "subject_id": "phy",
            "class_level": 11,
            "ncert_chapter_number": 8,
        },
        subtree=[
            {"node_id": "phy_11_ch8_g_variation", "type": "subtopic",
             "title": "Acceleration due to gravity", "parent_id": "phy_11_ch8_t1"},
            {"node_id": "c_height", "type": "concept", "title": "g with height",
             "parent_id": "phy_11_ch8_g_variation"},
            {"node_id": "c_depth", "type": "concept", "title": "g with depth",
             "parent_id": "phy_11_ch8_g_variation"},
        ],
        concepts=[
            {"node_id": "c_height", "type": "concept", "title": "g with height",
             "parent_id": "phy_11_ch8_g_variation"},
            {"node_id": "c_depth", "type": "concept", "title": "g with depth",
             "parent_id": "phy_11_ch8_g_variation"},
        ],
        subtopics=[
            {"node_id": "phy_11_ch8_g_variation", "type": "subtopic",
             "title": "Acceleration due to gravity", "parent_id": "phy_11_ch8_t1"},
        ],
        foundation_candidates=[
            {"node_id": "c_newton2", "title": "Newton's second law",
             "chapter_id": "phy_11_ch5", "chapter_title": "Laws of Motion",
             "source": "authored", "question_count": 22},
        ],
    )


def test_a_careless_extra_column_still_does_not_reach_the_payload():
    """Belt to the SQL's braces: the builder must not pass rows through wholesale.

    Every row here carries content in extra keys, as it would if somebody widened a
    SELECT without reading the module docstring. The payload must still hold none of it —
    the builder names what it copies, so a wider row is ignored rather than forwarded.
    """
    poison = "SUPERPOSITION-OF-COULOMB-FORCES-STEM"
    connection = _FakeConnection(
        by_fragment={
            "m.concept_node_id AS node_id": [
                {"node_id": "c_height", "question_type": "pyq", "difficulty": "medium",
                 "total": 12, "student_attempted": 4, "student_correct": 1,
                 "has_explanations": 9, "question_text": poison,
                 "correct_option_ids": [poison]},
            ],
            "FROM node_videos v": [
                {"youtube_id": "abc123XYZ01", "title": "Variation of g", "channel": "X",
                 "hangs_on_node_id": "c_height", "hangs_on_title": "g with height",
                 "depth": 3, "position": 0, "explanation_json": {"text": poison}},
            ],
            "FROM chapter_notes n": [
                {"chapter_id": "phy_11_ch8", "title": "Gravitation notes",
                 "page_count": 14, "pdf_url": f"https://cdn/{poison}.pdf"},
            ],
            "FROM tests t": [],
        },
        values={"COUNT(DISTINCT q.question_id)": 40},
    )
    inventory = asyncio.run(
        build_inventory(connection, _scope(), "JEENE_MASTER", proficiency="intermediate")
    )
    assert poison not in inventory.model_dump_json()


def test_the_student_block_is_empty_rather_than_invented_without_a_uid():
    """The debug view and a brand-new student are the same case, deliberately."""
    connection = _FakeConnection(values={"COUNT(DISTINCT q.question_id)": 40})
    inventory = asyncio.run(build_inventory(connection, _scope(), "JEENE_MASTER"))
    assert inventory.student.has_history is False
    assert inventory.student.scope_attempted == 0
    assert inventory.student.weak_concepts == []


def test_available_question_types_come_from_what_actually_exists():
    """The planner may only ask for a type the scope really has."""
    connection = _FakeConnection(
        by_fragment={
            "m.concept_node_id AS node_id": [
                {"node_id": "c_height", "question_type": "pyq", "difficulty": "medium",
                 "total": 12, "student_attempted": 0, "student_correct": 0,
                 "has_explanations": 0},
                {"node_id": "c_depth", "question_type": "mcq", "difficulty": "easy",
                 "total": 8, "student_attempted": 0, "student_correct": 0,
                 "has_explanations": 0},
            ],
        },
        values={"COUNT(DISTINCT q.question_id)": 20},
    )
    inventory = asyncio.run(build_inventory(connection, _scope(), "JEENE_MASTER"))
    assert inventory.constraints.available_question_types == ["mcq", "pyq"]


def test_scope_question_total_is_not_the_sum_of_the_buckets():
    """A question tagged to two concepts sits in two buckets and is still one question.

    Summing bucket totals would tell the planner a scope holds more than it does, and it
    would plan a checkpoint it cannot fill.
    """
    connection = _FakeConnection(
        by_fragment={
            "m.concept_node_id AS node_id": [
                {"node_id": "c_height", "question_type": "pyq", "difficulty": "medium",
                 "total": 12, "student_attempted": 0, "student_correct": 0,
                 "has_explanations": 0},
                {"node_id": "c_depth", "question_type": "pyq", "difficulty": "medium",
                 "total": 12, "student_attempted": 0, "student_correct": 0,
                 "has_explanations": 0},
            ],
        },
        values={"COUNT(DISTINCT q.question_id)": 15},
    )
    inventory = asyncio.run(build_inventory(connection, _scope(), "JEENE_MASTER"))
    assert sum(b.total for b in inventory.question_buckets) == 24
    assert inventory.scope_question_total == 15


# --- rollup ------------------------------------------------------------------------


def _bucket(node_id, qtype="pyq", difficulty="medium", total=1, **kw):
    return QuestionBucket(
        node_id=node_id, question_type=qtype, difficulty=difficulty, total=total, **kw
    )


def test_rollup_merges_concepts_into_their_subtopic():
    rolled = _roll_up(
        [
            _bucket("c_height", total=12, student_attempted=4, student_correct=1),
            _bucket("c_depth", total=8, student_attempted=2, student_correct=2),
        ],
        _scope(),
    )
    assert len(rolled) == 1
    assert rolled[0].node_id == "phy_11_ch8_g_variation"
    assert rolled[0].total == 20
    assert rolled[0].student_attempted == 6
    assert rolled[0].student_correct == 3


def test_rollup_keeps_types_and_difficulties_apart():
    """Merging a hard PYQ into an easy MCQ would make the catalogue a lie."""
    rolled = _roll_up(
        [
            _bucket("c_height", qtype="pyq", difficulty="hard", total=3),
            _bucket("c_depth", qtype="mcq", difficulty="easy", total=5),
        ],
        _scope(),
    )
    assert len(rolled) == 2
    assert {(b.question_type, b.difficulty) for b in rolled} == {
        ("pyq", "hard"), ("mcq", "easy")
    }


def test_rollup_leaves_an_unknown_concept_where_it_is():
    """A bucket whose concept is not in the subtree must not be silently reparented."""
    rolled = _roll_up([_bucket("c_stranger", total=4)], _scope())
    assert rolled[0].node_id == "c_stranger"


def test_a_small_scope_is_not_rolled_up():
    connection = _FakeConnection(
        by_fragment={
            "m.concept_node_id AS node_id": [
                {"node_id": "c_height", "question_type": "pyq", "difficulty": "medium",
                 "total": 12, "student_attempted": 0, "student_correct": 0,
                 "has_explanations": 0},
            ],
        },
        values={"COUNT(DISTINCT q.question_id)": 12},
    )
    inventory = asyncio.run(build_inventory(connection, _scope(), "JEENE_MASTER"))
    assert inventory.rollup.applied is False
    assert inventory.rollup.level == "concept"
    assert inventory.rollup.original_bucket_count == 1


def test_a_large_scope_is_rolled_up_and_says_so():
    """A thin plan over a huge chapter should be explainable afterwards."""
    rows = [
        {"node_id": f"c_{i}", "question_type": "pyq", "difficulty": "medium",
         "total": 2, "student_attempted": 0, "student_correct": 0,
         "has_explanations": 0}
        for i in range(_MAX_BUCKETS + 1)
    ]
    scope = _scope()
    scope.concepts = [
        {"node_id": f"c_{i}", "type": "concept", "title": f"Concept {i}",
         "parent_id": "phy_11_ch8_g_variation"}
        for i in range(_MAX_BUCKETS + 1)
    ]
    scope.subtree = scope.subtopics + scope.concepts
    connection = _FakeConnection(
        by_fragment={"m.concept_node_id AS node_id": rows},
        values={"COUNT(DISTINCT q.question_id)": 400},
    )
    inventory = asyncio.run(build_inventory(connection, scope, "JEENE_MASTER"))
    assert inventory.rollup.applied is True
    assert inventory.rollup.level == "subtopic"
    assert inventory.rollup.original_bucket_count == _MAX_BUCKETS + 1
    assert len(inventory.question_buckets) < _MAX_BUCKETS + 1


# --- constraints -------------------------------------------------------------------


@pytest.mark.parametrize(
    "proficiency,expected",
    [("basic", "easy"), ("intermediate", "medium"), ("advanced", "hard")],
)
def test_proficiency_sets_where_the_plan_aims(proficiency, expected):
    assert _TARGET_DIFFICULTY[proficiency] == expected


def test_an_unknown_proficiency_aims_at_the_middle_rather_than_failing():
    connection = _FakeConnection(values={"COUNT(DISTINCT q.question_id)": 10})
    inventory = asyncio.run(
        build_inventory(connection, _scope(), "JEENE_MASTER", proficiency=None)
    )
    assert inventory.constraints.target_difficulty == "medium"
    assert inventory.constraints.checkpoint_required is True


# --- scope -------------------------------------------------------------------------


def test_a_plan_is_only_ever_about_something_a_student_can_point_at():
    assert set(SCOPE_TYPES) == {"chapter", "topic", "subtopic"}


def test_closure_is_the_subtree_plus_its_groundwork():
    """JM-5 validates a plan's node references against exactly this set."""
    closure = _scope().closure_ids
    assert "c_height" in closure
    assert "c_newton2" in closure, "foundation must be referenceable"
    assert "c_stranger" not in closure


def test_the_prerequisite_walk_is_bounded():
    """`prerequisite_node_ids` is free text to Postgres, so a cycle is one typo away."""
    sql = scope_module._AUTHORED_PREREQUISITES_SQL
    assert f"c.depth < {scope_module._PREREQUISITE_DEPTH}" in sql
    assert scope_module._PREREQUISITE_DEPTH <= 5, "a deep walk is a slow one"
    # UNION, not UNION ALL: a diamond must collapse rather than fan out.
    assert re.search(r"\bUNION\b(?!\s+ALL)", sql)


def test_foundation_candidates_are_capped():
    """Groundwork is a step or two, not a second syllabus."""
    assert scope_module._MAX_FOUNDATION_CANDIDATES <= 8
    assert scope_module._MIN_FOUNDATION_QUESTIONS >= 1


def test_only_a_chapter_topic_or_subtopic_can_be_planned():
    """A concept is too small to be worth a plan; a subject is not a study session.

    The type check is what stops `POST /plans` on a concept producing a one-step plan
    that reads like a bug report.
    """
    from app.plans.scope import resolve_scope

    node = {
        "node_id": "c_height", "type": "concept", "title": "g with height",
        "parent_id": "phy_11_ch8_g_variation", "description": None,
        "subject_id": "phy", "class_level": 11,
    }
    connection = _FakeConnection(by_fragment={"FROM nodes n": [node]})
    assert asyncio.run(resolve_scope(connection, "c_height", "JEENE_MASTER")) is None


def test_an_unpublished_or_missing_node_has_no_scope():
    connection = _FakeConnection()
    assert asyncio.run(
        scope_module.resolve_scope(connection, "nope", "JEENE_MASTER")
    ) is None


def test_scope_resolution_scopes_every_query_to_the_caller_tenant():
    """Rule 6: nothing here may read across tenants, however the id was obtained."""
    node = {
        "node_id": "phy_11_ch8", "type": "chapter", "title": "Gravitation",
        "parent_id": None, "description": None, "subject_id": "phy",
        "class_level": 11, "ncert_chapter_number": 8, "pedagogical_notes": None,
        "prerequisite_node_ids": [], "estimated_minutes": None, "difficulty": None,
        "search_keywords": [], "depth": 2, "display_order": 0,
    }
    connection = _FakeConnection(by_fragment={"FROM nodes n": [node]})
    asyncio.run(scope_module.resolve_scope(connection, "phy_11_ch8", "JEENE_MASTER"))
    assert connection.queries, "no query ran"
    for query in connection.queries:
        assert "tenant_id = $1" in query, query[:120]
