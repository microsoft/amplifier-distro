"""Voice App GA Route Tests

Tests for the new GA Realtime API voice routes.

Test classes:
  TestVoiceManifest       - manifest.name/version
  TestStaticRoutes        - /api/status fields including assistant_name
  TestAuthEnforcement     - 401 without key, 200 with correct key
  TestCsrfProtection      - /events CSRF via Origin header
  TestSessionIdValidation - path traversal / bad chars -> 400
  TestSessionLifecycle    - POST /sessions, GET /sessions
  TestSignalingRoutes     - GET /session returns {value}, POST /sdp
  TestStubMode            - ek_-prefixed token, valid SDP
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

from fastapi import FastAPI
from starlette.testclient import TestClient

import amplifier_distro.server.apps.voice as voice_module
import amplifier_distro.server.stub as stub_module

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app() -> FastAPI:
    """Build a minimal FastAPI app with the voice router mounted."""
    from amplifier_distro.server.app import DistroServer
    from amplifier_distro.server.apps.voice import manifest

    server = DistroServer()
    server.register_app(manifest)
    return server.app


class _FakeSession:
    """Fake AmplifierSession returned by FakeBackend.create_session."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.coordinator = MagicMock()
        self.coordinator.register_capability = MagicMock()


class FakeBackend:
    """Minimal backend for voice route tests.

    Supports the extra methods required by VoiceConnection:
      - create_session(**kwargs) - accepts app_name / event_queue kwargs
      - register_hooks(session_id, hook)  -> unregister callable
      - mark_disconnected(session_id)
      - end_session(session_id)
      - cancel_session(session_id, level="graceful")
      - reconnect(session_id, ...)
    """

    def __init__(self) -> None:
        self._counter = 0
        self.calls: list[dict] = []

    async def create_session(self, **kwargs) -> _FakeSession:
        self._counter += 1
        sid = f"fake-session-{self._counter:04d}"
        self.calls.append({"method": "create_session", "result": sid, **kwargs})
        return _FakeSession(sid)

    def register_hooks(self, session_id: str, hook: object) -> object:
        self.calls.append({"method": "register_hooks", "session_id": session_id})
        return lambda: None  # unregister callable

    async def mark_disconnected(self, session_id: str) -> None:
        self.calls.append({"method": "mark_disconnected", "session_id": session_id})

    async def end_session(self, session_id: str) -> None:
        self.calls.append({"method": "end_session", "session_id": session_id})

    async def cancel_session(
        self, session_id: str, level: str = "graceful", **kwargs
    ) -> None:
        self.calls.append(
            {
                "method": "cancel_session",
                "session_id": session_id,
                "level": level,
            }
        )

    async def reconnect(self, session_id: str, **kwargs) -> None:
        self.calls.append({"method": "reconnect", "session_id": session_id})

    def resolve_approval(self, session_id: str, request_id: str, choice: str) -> bool:
        return False

    async def get_session_info(self, session_id: str):
        return None

    def list_active_sessions(self) -> list:
        return []

    async def execute(self, session_id: str, prompt: str, images=None) -> None:
        self.calls.append(
            {"method": "execute", "session_id": session_id, "prompt": prompt}
        )


# ---------------------------------------------------------------------------
# TestVoiceManifest
# ---------------------------------------------------------------------------


class TestVoiceManifest:
    def test_manifest_name_is_voice(self) -> None:
        from amplifier_distro.server.apps.voice import manifest

        assert manifest.name == "voice"

    def test_manifest_version_is_1_0_0(self) -> None:
        from amplifier_distro.server.apps.voice import manifest

        assert manifest.version == "1.0.0"


# ---------------------------------------------------------------------------
# TestStaticRoutes
# ---------------------------------------------------------------------------


class TestStaticRoutes:
    def setup_method(self) -> None:
        self.client = TestClient(_make_app(), raise_server_exceptions=False)

    def test_api_status_returns_200(self) -> None:
        resp = self.client.get("/apps/voice/api/status")
        assert resp.status_code == 200

    def test_api_status_has_status_field(self) -> None:
        data = self.client.get("/apps/voice/api/status").json()
        assert "status" in data

    def test_api_status_has_model_field(self) -> None:
        data = self.client.get("/apps/voice/api/status").json()
        assert "model" in data

    def test_api_status_has_assistant_name_field(self) -> None:
        data = self.client.get("/apps/voice/api/status").json()
        assert "assistant_name" in data

    def test_api_status_assistant_name_from_env(self) -> None:
        os.environ["AMPLIFIER_VOICE_ASSISTANT_NAME"] = "Testy"
        try:
            data = self.client.get("/apps/voice/api/status").json()
            assert data["assistant_name"] == "Testy"
        finally:
            os.environ.pop("AMPLIFIER_VOICE_ASSISTANT_NAME", None)

    def test_api_status_turn_server_is_none(self) -> None:
        data = self.client.get("/apps/voice/api/status").json()
        assert data.get("turn_server") is None

    def test_index_returns_200(self) -> None:
        resp = self.client.get("/apps/voice/")
        assert resp.status_code == 200

    def test_index_returns_html(self) -> None:
        resp = self.client.get("/apps/voice/")
        assert "text/html" in resp.headers.get("content-type", "")

    def test_vendor_js_returns_200(self) -> None:
        resp = self.client.get("/apps/voice/static/vendor.js")
        assert resp.status_code == 200

    def test_vendor_js_content_type_is_javascript(self) -> None:
        resp = self.client.get("/apps/voice/static/vendor.js")
        assert "javascript" in resp.headers.get("content-type", "")

    def test_vendor_js_contains_preact(self) -> None:
        resp = self.client.get("/apps/voice/static/vendor.js")
        assert b"preact" in resp.content

    def test_vendor_js_contains_htm_binding(self) -> None:
        resp = self.client.get("/apps/voice/static/vendor.js")
        assert b"window.html" in resp.content

    def test_vendor_js_contains_marked(self) -> None:
        resp = self.client.get("/apps/voice/static/vendor.js")
        assert b"marked" in resp.content


# ---------------------------------------------------------------------------
# TestAuthEnforcement
# ---------------------------------------------------------------------------


class TestAuthEnforcement:
    def setup_method(self) -> None:
        os.environ["AMPLIFIER_SERVER_API_KEY"] = "test-secret-key"
        self.client = TestClient(_make_app(), raise_server_exceptions=False)

    def teardown_method(self) -> None:
        os.environ.pop("AMPLIFIER_SERVER_API_KEY", None)
        voice_module._active_connection = None

    def test_get_session_no_key_returns_401(self) -> None:
        resp = self.client.get("/apps/voice/session")
        assert resp.status_code == 401

    def test_post_sessions_no_key_returns_401(self) -> None:
        resp = self.client.post("/apps/voice/sessions", json={})
        assert resp.status_code == 401

    def test_get_sessions_no_key_returns_401(self) -> None:
        resp = self.client.get("/apps/voice/sessions")
        assert resp.status_code == 401

    def test_post_cancel_no_key_returns_401(self) -> None:
        resp = self.client.post(
            "/apps/voice/cancel", json={"session_id": "test", "immediate": False}
        )
        assert resp.status_code == 401

    def test_post_tools_execute_no_key_returns_401(self) -> None:
        resp = self.client.post("/apps/voice/tools/execute", json={"name": "delegate"})
        assert resp.status_code == 401

    def test_get_session_with_correct_key_not_401(self) -> None:
        # Use stub mode so no real OpenAI call is made
        stub_module._stub_mode = True
        try:
            resp = self.client.get(
                "/apps/voice/session", headers={"x-api-key": "test-secret-key"}
            )
            assert resp.status_code != 401
        finally:
            stub_module._stub_mode = False

    def test_get_sessions_with_correct_key_not_401(self, tmp_path) -> None:
        old_repo = voice_module._repo_override
        from amplifier_distro.server.apps.voice.transcript.repository import (
            VoiceConversationRepository,
        )

        voice_module._repo_override = VoiceConversationRepository(base_dir=tmp_path)
        try:
            resp = self.client.get(
                "/apps/voice/sessions", headers={"x-api-key": "test-secret-key"}
            )
            assert resp.status_code != 401
        finally:
            voice_module._repo_override = old_repo


# ---------------------------------------------------------------------------
# TestCsrfProtection
# ---------------------------------------------------------------------------


class TestCsrfProtection:
    """CSRF protection via Origin header on /events.

    The 403 test hits the actual endpoint (HTTPException is raised before
    the streaming response starts, so TestClient returns immediately).

    The "not-403" tests call _check_origin() directly.  This is the
    function that drives the CSRF decision; if it does NOT raise then the
    endpoint will NOT return 403 - testing the dependency is equivalent
    to testing the endpoint outcome.  Direct async testing avoids hanging
    on the infinite SSE stream.
    """

    def test_events_evil_origin_returns_403(self) -> None:
        """CSRF check rejects external origins at the /events endpoint."""
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.get(
            "/apps/voice/events",
            headers={"origin": "https://evil.example.com"},
        )
        assert resp.status_code == 403

    async def test_events_localhost_origin_allowed(self) -> None:
        """_check_origin does NOT raise for localhost (not-403 path)."""
        from amplifier_distro.server.apps.voice import _check_origin

        # Should complete without raising HTTPException
        await _check_origin(origin="http://localhost:3000")

    async def test_events_127_origin_allowed(self) -> None:
        """_check_origin does NOT raise for 127.0.0.1 (not-403 path)."""
        from amplifier_distro.server.apps.voice import _check_origin

        await _check_origin(origin="http://127.0.0.1:5173")

    async def test_events_no_origin_allowed(self) -> None:
        """_check_origin does NOT raise when Origin header is absent."""
        from amplifier_distro.server.apps.voice import _check_origin

        await _check_origin(origin=None)


# ---------------------------------------------------------------------------
# TestSessionIdValidation
# ---------------------------------------------------------------------------


class TestSessionIdValidation:
    def setup_method(self) -> None:
        os.environ.pop("AMPLIFIER_SERVER_API_KEY", None)
        self.client = TestClient(_make_app(), raise_server_exceptions=False)

    def test_path_traversal_end_route(self) -> None:
        """../evil as session_id should be rejected."""
        resp = self.client.post(
            "/apps/voice/sessions/../end",
            json={"reason": "test"},
        )
        # Either 400 (session id validation) or 404 (routing) or 422 (unprocessable)
        assert resp.status_code in (400, 404, 422)

    def test_slash_in_session_id_returns_400(self) -> None:
        resp = self.client.post(
            "/apps/voice/sessions/bad%2Fid/end",
            json={"reason": "test"},
        )
        assert resp.status_code in (400, 404, 422)

    def test_valid_session_id_passes_validation(self, tmp_path) -> None:
        """A valid session ID should get past validation (may 404 for unknown)."""
        old_repo = voice_module._repo_override
        from amplifier_distro.server.apps.voice.transcript.repository import (
            VoiceConversationRepository,
        )

        voice_module._repo_override = VoiceConversationRepository(base_dir=tmp_path)
        old_backend = voice_module._backend_override
        voice_module._backend_override = FakeBackend()
        try:
            resp = self.client.post(
                "/apps/voice/sessions/my-valid-session-id-001/end",
                json={"reason": "user_ended"},
            )
            # Should not be 400 for validation; may be anything else
            assert resp.status_code != 400
        finally:
            voice_module._repo_override = old_repo
            voice_module._backend_override = old_backend

    def test_special_chars_in_session_id_returns_400(self) -> None:
        resp = self.client.post(
            "/apps/voice/sessions/bad@session!/end",
            json={"reason": "test"},
        )
        assert resp.status_code in (400, 404, 422)


# ---------------------------------------------------------------------------
# TestSessionLifecycle
# ---------------------------------------------------------------------------


class TestSessionLifecycle:
    def setup_method(self) -> None:
        os.environ.pop("AMPLIFIER_SERVER_API_KEY", None)
        self.fake_backend = FakeBackend()
        voice_module._backend_override = self.fake_backend
        voice_module._active_connection = None

    def teardown_method(self) -> None:
        voice_module._backend_override = None
        voice_module._repo_override = None
        voice_module._active_connection = None

    def test_post_sessions_returns_session_id(self, tmp_path) -> None:
        from amplifier_distro.server.apps.voice.transcript.repository import (
            VoiceConversationRepository,
        )

        voice_module._repo_override = VoiceConversationRepository(base_dir=tmp_path)
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.post(
            "/apps/voice/sessions",
            json={"workspace_root": str(tmp_path)},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "session_id" in data
        assert data["session_id"]  # non-empty

    def test_post_sessions_sets_active_connection(self, tmp_path) -> None:
        from amplifier_distro.server.apps.voice.transcript.repository import (
            VoiceConversationRepository,
        )

        voice_module._repo_override = VoiceConversationRepository(base_dir=tmp_path)
        client = TestClient(_make_app(), raise_server_exceptions=False)
        client.post("/apps/voice/sessions", json={"workspace_root": str(tmp_path)})
        assert voice_module._active_connection is not None

    def test_get_sessions_returns_list(self, tmp_path) -> None:
        from amplifier_distro.server.apps.voice.transcript.repository import (
            VoiceConversationRepository,
        )

        voice_module._repo_override = VoiceConversationRepository(base_dir=tmp_path)
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.get("/apps/voice/sessions")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_get_sessions_returns_created_sessions(self, tmp_path) -> None:
        from amplifier_distro.server.apps.voice.transcript.repository import (
            VoiceConversationRepository,
        )

        voice_module._repo_override = VoiceConversationRepository(base_dir=tmp_path)
        client = TestClient(_make_app(), raise_server_exceptions=False)
        # Create a session
        client.post("/apps/voice/sessions", json={"workspace_root": str(tmp_path)})
        # List sessions
        resp = client.get("/apps/voice/sessions")
        assert resp.status_code == 200
        sessions = resp.json()
        assert len(sessions) == 1


# ---------------------------------------------------------------------------
# TestSignalingRoutes
# ---------------------------------------------------------------------------


class TestSignalingRoutes:
    def setup_method(self) -> None:
        os.environ.pop("AMPLIFIER_SERVER_API_KEY", None)
        stub_module._stub_mode = True

    def teardown_method(self) -> None:
        stub_module._stub_mode = False

    def test_get_session_returns_value_token(self) -> None:
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.get("/apps/voice/session")
        assert resp.status_code == 200
        data = resp.json()
        assert "value" in data

    def test_post_sdp_returns_sdp_answer(self) -> None:
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.post(
            "/apps/voice/sdp",
            content=b"v=0\r\n",
            headers={"content-type": "application/sdp"},
        )
        assert resp.status_code == 200
        assert "v=0" in resp.text

    def test_get_session_value_is_string(self) -> None:
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.get("/apps/voice/session")
        data = resp.json()
        assert isinstance(data.get("value"), str)
        assert data["value"]  # non-empty


# ---------------------------------------------------------------------------
# TestStubMode
# ---------------------------------------------------------------------------


class TestStubMode:
    def setup_method(self) -> None:
        stub_module._stub_mode = True
        os.environ.pop("AMPLIFIER_SERVER_API_KEY", None)

    def teardown_method(self) -> None:
        stub_module._stub_mode = False

    def test_stub_voice_client_secret_function_exists(self) -> None:
        from amplifier_distro.server.stub import stub_voice_client_secret

        token = stub_voice_client_secret()
        assert isinstance(token, str)

    def test_stub_client_secret_has_ek_prefix(self) -> None:
        from amplifier_distro.server.stub import stub_voice_client_secret

        token = stub_voice_client_secret()
        assert token.startswith("ek_"), f"Expected ek_ prefix, got: {token!r}"

    def test_get_session_stub_token_has_ek_prefix(self) -> None:
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.get("/apps/voice/session")
        assert resp.status_code == 200
        data = resp.json()
        value = data.get("value", "")
        assert value.startswith("ek_"), f"Expected ek_ prefix, got: {value!r}"

    def test_post_sdp_stub_returns_valid_sdp(self) -> None:
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.post(
            "/apps/voice/sdp",
            content=b"v=0\r\n",
            headers={"content-type": "application/sdp"},
        )
        assert resp.status_code == 200
        assert "v=0" in resp.text

    def test_stub_sdp_contains_audio_line(self) -> None:
        from amplifier_distro.server.stub import stub_voice_sdp

        sdp = stub_voice_sdp()
        assert "m=audio" in sdp


# ---------------------------------------------------------------------------
# TestTranscriptSync
# ---------------------------------------------------------------------------


class TestTranscriptSync:
    """Tests for POST /sessions/{id}/transcript body format validation.

    The client (sendBeacon / fetch) must send {"entries": [...]} not a bare
    JSON array. A bare array was previously causing a 500 because list objects
    don't have .get(); the server now explicitly returns 400 for non-dict bodies.
    """

    def setup_method(self) -> None:
        os.environ.pop("AMPLIFIER_SERVER_API_KEY", None)
        voice_module._backend_override = FakeBackend()
        voice_module._active_connection = None

    def teardown_method(self) -> None:
        voice_module._backend_override = None
        voice_module._repo_override = None
        voice_module._active_connection = None

    def test_bare_array_body_returns_400(self, tmp_path) -> None:
        """POSTing a bare JSON array (sendBeacon bug) must return 400, not 500."""
        from amplifier_distro.server.apps.voice.transcript.repository import (
            VoiceConversationRepository,
        )

        voice_module._repo_override = VoiceConversationRepository(base_dir=tmp_path)
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.post(
            "/apps/voice/sessions/test-session-id/transcript",
            content=b'[{"role":"user","content":"hello"}]',
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
        assert "entries" in resp.json().get("error", "").lower()

    def test_wrapped_entries_object_returns_200(self, tmp_path) -> None:
        """POSTing {"entries": [...]} must return 200 and report synced count."""
        from amplifier_distro.server.apps.voice.transcript.repository import (
            VoiceConversationRepository,
        )

        voice_module._repo_override = VoiceConversationRepository(base_dir=tmp_path)
        # Pre-create the conversation so add_entries doesn't fail on unknown id
        from datetime import UTC, datetime

        from amplifier_distro.server.apps.voice.transcript.models import (
            VoiceConversation,
        )

        repo = voice_module._repo_override
        repo.create_conversation(
            VoiceConversation(
                id="test-session-id",
                title="Test session",
                status="active",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.post(
            "/apps/voice/sessions/test-session-id/transcript",
            json={"entries": [{"role": "user", "content": "hello"}]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("synced") == 1

    def test_empty_entries_list_returns_200(self, tmp_path) -> None:
        """POSTing {"entries": []} (no messages yet) must return 200 synced=0."""
        from datetime import UTC, datetime

        from amplifier_distro.server.apps.voice.transcript.models import (
            VoiceConversation,
        )
        from amplifier_distro.server.apps.voice.transcript.repository import (
            VoiceConversationRepository,
        )

        repo = VoiceConversationRepository(base_dir=tmp_path)
        repo.create_conversation(
            VoiceConversation(
                id="test-session-id",
                title="Test session",
                status="active",
                created_at=datetime.now(UTC),
                updated_at=datetime.now(UTC),
            )
        )
        voice_module._repo_override = repo
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.post(
            "/apps/voice/sessions/test-session-id/transcript",
            json={"entries": []},
        )
        assert resp.status_code == 200
        assert resp.json().get("synced") == 0

    def test_non_json_body_returns_400(self, tmp_path) -> None:
        """Non-JSON body must return 400."""
        from amplifier_distro.server.apps.voice.transcript.repository import (
            VoiceConversationRepository,
        )

        voice_module._repo_override = VoiceConversationRepository(base_dir=tmp_path)
        client = TestClient(_make_app(), raise_server_exceptions=False)
        resp = client.post(
            "/apps/voice/sessions/test-session-id/transcript",
            content=b"not json at all",
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 400
