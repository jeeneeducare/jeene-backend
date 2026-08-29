"""Test-wide safety rails.

One rule, and it exists because it was broken within a minute of the planner being wired
up: **no test calls a provider unless it says so.** A developer with a working `.env` ran
the unit suite and a test that had asserted "the deterministic planner ran" quietly spent
fifteen seconds and real money proving the opposite.

Money is the smaller half. A suite that reaches the network is a suite that fails on a
train, gives different answers on different days, and cannot be trusted to say what broke.
"""

import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def no_provider_calls_by_default(request, monkeypatch):
    """Disable the planner for every test that has not opted in.

    Opt in with `@pytest.mark.uses_provider`, which nothing in the default suite does —
    the provider is exercised by a fake, and against the real API only by hand.
    """
    if request.node.get_closest_marker("uses_provider"):
        return
    monkeypatch.setattr(settings, "jeene_planner_enabled", False, raising=False)
    monkeypatch.setattr(settings, "openai_api_key", None, raising=False)

    # The provider is built once and cached, so a cache warmed by an earlier test would
    # walk straight past the settings above.
    from app.routers import plans as plans_router

    plans_router._planner_provider.cache_clear()
    yield
    plans_router._planner_provider.cache_clear()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "uses_provider: this test may call a real model provider (costs money, needs a key)",
    )
