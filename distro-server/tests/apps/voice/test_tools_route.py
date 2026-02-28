"""Tests for the /tools/execute route in the voice app."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import amplifier_distro.server.apps.voice as voice_module
from amplifier_distro.server.apps.voice import router
from amplifier_distro.server.session_backend import MockBackend, SessionInfo


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/apps/voice")
    return app


class _MockConnection:
    """Minimal stand-in for VoiceConnection with a known session_id."""

    session_id = "test-session-001"
    project_id = "test-project"


@pytest.fixture(autouse=True)
def reset_voice_module():
    """Restore module-level overrides after every test."""
    yield
    voice_module._backend_override = None
    voice_module._active_connection = None
    voice_module._repo_override = None


def _setup_backend_with_session() -> MockBackend:
    """Return a MockBackend that knows about test-session-001."""
    backend = MockBackend()
    # Insert a live session record directly so send_message won't raise.
    backend._sessions["test-session-001"] = SessionInfo(
        session_id="test-session-001",
        project_id="test-project",
        working_dir="~",
        is_active=True,
    )
    backend._message_history["test-session-001"] = []
    return backend


def test_delegate_tool_returns_actual_backend_result():
    """The delegate tool must return the real send_message response, not 'delegated'.

    RED: fails before the fix because the route returns {"result": "delegated"}.
    GREEN: passes after the fix because the route returns the MockBackend echo response.
    """
    backend = _setup_backend_with_session()
    voice_module._backend_override = backend
    voice_module._active_connection = _MockConnection()

    app = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/apps/voice/tools/execute",
            json={"name": "delegate", "arguments": {"instruction": "list my files"}},
        )

    assert resp.status_code == 200
    data = resp.json()

    # The hardcoded value that existed before the fix — must NOT appear after.
    assert data.get("result") != "delegated", (
        "Route still returning hardcoded 'delegated'. Apply the fix to __init__.py."
    )

    # MockBackend.send_message echoes back "[Mock response to: <message>]".
    assert "list my files" in data.get("result", ""), (
        f"Expected the actual backend response in 'result', got: {data}"
    )


def test_delegate_tool_no_active_session_returns_400():
    """If there is no active connection, the route must return 400."""
    backend = _setup_backend_with_session()
    voice_module._backend_override = backend
    voice_module._active_connection = None  # explicitly no session

    app = _make_app()
    with TestClient(app) as client:
        resp = client.post(
            "/apps/voice/tools/execute",
            json={"name": "delegate", "arguments": {"instruction": "do something"}},
        )

    assert resp.status_code == 400
    assert "No active voice session" in resp.json().get("error", "")
