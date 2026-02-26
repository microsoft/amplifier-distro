"""Chat App Acceptance Tests — Skeleton"""

from __future__ import annotations

import pytest
from fastapi import APIRouter
from starlette.testclient import TestClient

from amplifier_distro.server.app import AppManifest, DistroServer
from amplifier_distro.server.services import init_services, reset_services


@pytest.fixture(autouse=True)
def _clean_services():
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


class TestChatManifest:
    def test_manifest_name_is_chat(self):
        from amplifier_distro.server.apps.chat import manifest

        assert manifest.name == "chat"

    def test_manifest_has_router(self):
        from amplifier_distro.server.apps.chat import manifest

        assert isinstance(manifest.router, APIRouter)

    def test_manifest_is_app_manifest_type(self):
        from amplifier_distro.server.apps.chat import manifest

        assert isinstance(manifest, AppManifest)


class TestChatIndexEndpoint:
    def test_index_returns_200(self, chat_client):
        r = chat_client.get("/apps/chat/")
        assert r.status_code == 200

    def test_index_returns_html(self, chat_client):
        r = chat_client.get("/apps/chat/")
        assert "text/html" in r.headers["content-type"]

    def test_index_contains_amplifier(self, chat_client):
        r = chat_client.get("/apps/chat/")
        assert "Amplifier" in r.text


class TestChatHealthEndpoint:
    def test_health_returns_ok(self, chat_client):
        r = chat_client.get("/apps/chat/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


class TestChatVendorEndpoint:
    def test_vendor_js_returns_200(self, chat_client):
        r = chat_client.get("/apps/chat/vendor.js")
        assert r.status_code == 200

    def test_vendor_js_content_type(self, chat_client):
        r = chat_client.get("/apps/chat/vendor.js")
        assert "javascript" in r.headers["content-type"]


class TestChatWebSocketEndpoint:
    def test_websocket_accepts_connection(self, chat_client):
        """WebSocket at /apps/chat/ws accepts connections."""
        with chat_client.websocket_connect("/apps/chat/ws") as ws:
            ws.send_json({"type": "ping"})
            msg = ws.receive_json()
            assert msg["type"] == "pong"

    def test_websocket_create_session(self, chat_client):
        """create_session message returns session_created."""
        with chat_client.websocket_connect("/apps/chat/ws") as ws:
            ws.send_json(
                {
                    "type": "create_session",
                    "cwd": "~",
                    "bundle": None,
                }
            )
            msg = ws.receive_json()
            assert msg["type"] == "session_created"
            assert isinstance(msg["session_id"], str) and msg["session_id"]


class TestChatSessionsAPI:
    def test_list_sessions_returns_200(self, chat_client):
        r = chat_client.get("/apps/chat/api/sessions")
        assert r.status_code == 200

    def test_list_sessions_returns_list(self, chat_client):
        r = chat_client.get("/apps/chat/api/sessions")
        data = r.json()
        assert "sessions" in data
        assert isinstance(data["sessions"], list)

    def test_list_sessions_empty_when_none(self, chat_client):
        r = chat_client.get("/apps/chat/api/sessions")
        assert r.json()["sessions"] == []

    def test_list_sessions_returns_session_shape(self, chat_client):
        """Each session entry has the four required fields with correct types."""
        from unittest.mock import patch

        from amplifier_distro.server.session_backend import SessionInfo

        fake = SessionInfo(
            session_id="abc-123",
            working_dir="/tmp/work",
            description="test session",
            is_active=True,
        )
        with patch(
            "amplifier_distro.server.session_backend.MockBackend.list_active_sessions",
            return_value=[fake],
        ):
            r = chat_client.get("/apps/chat/api/sessions")
            sessions = r.json()["sessions"]
            assert len(sessions) == 1
            s = sessions[0]
            assert s["session_id"] == "abc-123"
            assert s["working_dir"] == "/tmp/work"
            assert s["description"] == "test session"
            assert s["is_active"] is True

    def test_list_sessions_returns_empty_when_services_unavailable(self, chat_client):
        """Returns empty list (not a 500) when get_services() raises."""
        from unittest.mock import patch

        # get_services is a lazy import inside the function, so patch at the source
        with patch(
            "amplifier_distro.server.services.get_services",
            side_effect=RuntimeError("Services down"),
        ):
            r = chat_client.get("/apps/chat/api/sessions")
            assert r.status_code == 200
            assert r.json() == {"sessions": []}


class TestChatTranscriptAPI:
    def test_transcript_404_for_unknown_session(self, chat_client):
        r = chat_client.get("/apps/chat/api/sessions/no-such-session/transcript")
        assert r.status_code == 404

    def test_transcript_returns_messages(self, chat_client, tmp_path, monkeypatch):
        """Transcript JSONL is parsed and returned as array."""
        import json

        session_id = "test-transcript-session"
        session_dir = tmp_path / "projects" / "test-proj" / "sessions" / session_id
        session_dir.mkdir(parents=True)
        transcript = session_dir / "transcript.jsonl"
        lines = [
            json.dumps({"role": "user", "content": "hello"}),
            json.dumps({"role": "assistant", "content": "hi there"}),
        ]
        transcript.write_text("\n".join(lines))

        # Patch AMPLIFIER_HOME to point at tmp_path
        monkeypatch.setattr(
            "amplifier_distro.server.apps.chat.AMPLIFIER_HOME",
            str(tmp_path),
        )

        r = chat_client.get(f"/apps/chat/api/sessions/{session_id}/transcript")
        assert r.status_code == 200
        data = r.json()
        assert data["session_id"] == session_id
        assert len(data["transcript"]) == 2
        assert data["transcript"][0]["role"] == "user"

    def test_transcript_400_for_invalid_session_id(self, chat_client):
        """Returns 400 when session_id contains characters outside the allowed set.

        Dots, percent signs, and similar chars are rejected to prevent
        path-traversal payloads from reaching the filesystem.
        """
        # Dots are not in [a-zA-Z0-9_-] so this reaches the handler but fails
        # the regex guard (Starlette cannot normalize dots-in-segment away).
        r = chat_client.get("/apps/chat/api/sessions/bad.session.id/transcript")
        assert r.status_code == 400
        assert r.json()["error"] == "Invalid session ID format"

    def test_transcript_500_on_unreadable_file(
        self, chat_client, tmp_path, monkeypatch
    ):
        """Returns 500 when transcript file cannot be read."""
        session_id = "bad-perms-session"
        session_dir = tmp_path / "projects" / "proj" / "sessions" / session_id
        session_dir.mkdir(parents=True)
        tf = session_dir / "transcript.jsonl"
        tf.write_text('{"role": "user", "content": "hi"}')
        tf.chmod(0o000)  # make unreadable

        monkeypatch.setattr(
            "amplifier_distro.server.apps.chat.AMPLIFIER_HOME", str(tmp_path)
        )
        try:
            r = chat_client.get(f"/apps/chat/api/sessions/{session_id}/transcript")
            assert r.status_code == 500
        finally:
            tf.chmod(0o644)  # restore for cleanup

    def test_transcript_empty_file_returns_empty_list(
        self, chat_client, tmp_path, monkeypatch
    ):
        """Empty transcript.jsonl returns empty transcript array, not 500."""
        session_id = "empty-transcript"
        session_dir = tmp_path / "projects" / "proj" / "sessions" / session_id
        session_dir.mkdir(parents=True)
        (session_dir / "transcript.jsonl").write_text("")  # empty file

        monkeypatch.setattr(
            "amplifier_distro.server.apps.chat.AMPLIFIER_HOME", str(tmp_path)
        )
        r = chat_client.get(f"/apps/chat/api/sessions/{session_id}/transcript")
        assert r.status_code == 200
        assert r.json()["transcript"] == []

    def test_transcript_filters_non_role_entries(
        self, chat_client, tmp_path, monkeypatch
    ):
        """Entries without 'role' key and corrupt JSON lines are filtered out."""
        import json as _json

        session_id = "filter-session"
        session_dir = tmp_path / "projects" / "proj" / "sessions" / session_id
        session_dir.mkdir(parents=True)
        lines = [
            _json.dumps({"role": "user", "content": "hello"}),
            _json.dumps({"type": "system_event", "data": "no role key"}),
            "not valid json at all",
            _json.dumps({"role": "assistant", "content": "hi"}),
        ]
        (session_dir / "transcript.jsonl").write_text("\n".join(lines))
        monkeypatch.setattr(
            "amplifier_distro.server.apps.chat.AMPLIFIER_HOME", str(tmp_path)
        )
        r = chat_client.get(f"/apps/chat/api/sessions/{session_id}/transcript")
        assert r.status_code == 200
        assert len(r.json()["transcript"]) == 2  # only the two with role keys
