import os

import pytest

from app.routers.attempts import _accuracy, _grade
from app.schemas import AttemptRequest


class FakeQuestion(dict):
    """asyncpg.Record stand-in: _grade only reads it by key."""


def _mcq(correct):
    return FakeQuestion(
        question_type="mcq",
        correct_option_ids=correct,
        numerical_answer=None,
        numerical_tolerance=None,
    )


def _numerical(answer, tolerance):
    return FakeQuestion(
        question_type="numerical",
        correct_option_ids=None,
        numerical_answer=answer,
        numerical_tolerance=tolerance,
    )


def _req(**kwargs):
    return AttemptRequest(attempt_id="00000000-0000-0000-0000-000000000000",
                          question_id="q", **kwargs)


# --- grading is server-side and must never trust the client ---


def test_mcq_correct_when_selection_matches_key():
    assert _grade(_mcq(["b"]), _req(selected_option_ids=["b"])) is True


def test_mcq_wrong_selection():
    assert _grade(_mcq(["b"]), _req(selected_option_ids=["a"])) is False


def test_mcq_ignores_option_order():
    assert _grade(_mcq(["a", "c"]), _req(selected_option_ids=["c", "a"])) is True


def test_mcq_partial_selection_is_not_correct():
    assert _grade(_mcq(["a", "c"]), _req(selected_option_ids=["a"])) is False


def test_mcq_empty_selection_is_not_correct():
    assert _grade(_mcq(["b"]), _req(selected_option_ids=[])) is False


def test_question_without_a_key_is_never_correct():
    assert _grade(_mcq([]), _req(selected_option_ids=["a"])) is False


def test_numerical_within_tolerance():
    assert _grade(_numerical(9.8, 0.1), _req(numeric_answer=9.75)) is True


def test_numerical_outside_tolerance():
    assert _grade(_numerical(9.8, 0.1), _req(numeric_answer=9.5)) is False


def test_numerical_exact_when_no_tolerance():
    assert _grade(_numerical(5, None), _req(numeric_answer=5.0)) is True
    assert _grade(_numerical(5, None), _req(numeric_answer=5.01)) is False


def test_numerical_missing_answer_is_not_correct():
    assert _grade(_numerical(9.8, 0.1), _req()) is False


def test_accuracy_handles_no_attempts():
    assert _accuracy(0, 0) == 0.0
    assert _accuracy(1, 2) == 0.5


# --- the endpoints require a signed-in user ---

integration = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="DATABASE_URL not set; needs the DB + app startup",
)


@pytest.fixture(scope="module")
def client():
    from starlette.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


@integration
def test_recording_an_attempt_requires_auth(client):
    body = {"attempt_id": "00000000-0000-0000-0000-000000000001", "question_id": "q"}
    assert client.post("/attempts", json=body).status_code == 401


@integration
def test_progress_requires_auth(client):
    assert client.get("/progress").status_code == 401
    assert client.get("/progress/chapters/phy_11_ch4").status_code == 401
