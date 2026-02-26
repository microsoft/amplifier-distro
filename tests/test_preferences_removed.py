"""RED test: /api/preferences endpoints must not exist after removal.

This test verifies that chat-preferences.json support has been fully removed:
- GET /apps/chat/api/preferences returns 404
- PUT /apps/chat/api/preferences returns 404
- preferences module is not importable from chat package
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from amplifier_distro.server.app import DistroServer
from amplifier_distro.server.services import init_services, reset_services


@pytest.fixture(autouse=True)
def _clean():
    reset_services()
    yield
    reset_services()


@pytest.fixture
def chat_client() -> TestClient:
    init_services(dev_mode=True)
    from amplifier_distro.server.apps.chat import manifest

    server = DistroServer()
    server.register_app(manifest)
    return TestClient(server.app)


def test_get_preferences_is_gone(chat_client):
    """GET /api/preferences must return 404 — endpoint removed."""
    r = chat_client.get("/apps/chat/api/preferences")
    assert r.status_code == 404


def test_put_preferences_is_gone(chat_client):
    """PUT /api/preferences must return 404 — endpoint removed."""
    r = chat_client.put("/apps/chat/api/preferences", json={})
    assert r.status_code == 404


def test_preferences_module_not_imported_in_chat():
    """load_preferences / save_preferences must not exist in the chat namespace."""
    import amplifier_distro.server.apps.chat as chat_mod

    assert not hasattr(chat_mod, "load_preferences"), (
        "load_preferences should not be in chat module namespace"
    )
    assert not hasattr(chat_mod, "save_preferences"), (
        "save_preferences should not be in chat module namespace"
    )
