import os

import pytest

# Auth verification against a live Firebase token is covered by a manual e2e script
# (mint token -> /auth/session -> DB). These check the parts that need no real token:
# the 401 gates and that content stays anonymous (auth-optional).

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
def test_auth_session_requires_token(client):
    assert client.post("/auth/session", json={}).status_code == 401


@integration
def test_auth_me_requires_token(client):
    assert client.get("/auth/me").status_code == 401


@integration
def test_auth_me_rejects_bogus_token(client):
    r = client.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


@integration
def test_content_is_auth_optional(client):
    # No token -> content still served (scoped to JEENE_MASTER).
    assert client.get("/chapters").status_code == 200
